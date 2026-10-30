#!/usr/bin/env python3
"""
Sincroniza automaticamente tomadas de duas câmeras usando o áudio
(correlação cruzada). Cada câmera fica numa pasta separada; o script
descobre sozinho qual arquivo da câmera A corresponde a qual arquivo da
câmera B (pela força da correlação de áudio) e gera cópias já cortadas
para que cada par comece exatamente no mesmo instante.

Uso:
    python3 sync_videos.py <pasta_camera_A> <pasta_camera_B> <pasta_saida> [opções]

Requisitos:
    - ffmpeg / ffprobe no PATH
    - pip install numpy scipy
"""

import argparse
import csv
import json
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.signal import correlate, correlation_lags

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".mxf"}


class Cancelled(Exception):
    """Levantada quando o usuário cancela a sincronização em andamento."""


def check_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit(
            "ffmpeg/ffprobe não encontrados no PATH.\n"
            "Instale com: brew install ffmpeg"
        )


def write_progress(output_dir: Path, text: str):
    try:
        (output_dir / "progress.txt").write_text(text)
    except OSError:
        pass


def list_video_files(input_dir: Path):
    files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    if not files:
        sys.exit(f"Nenhum vídeo encontrado em {input_dir}")
    return files


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def source_bitrate(path: Path, floor=8_000_000, ceiling=140_000_000) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    try:
        bitrate = int(json.loads(out.stdout)["format"]["bit_rate"])
    except (KeyError, TypeError, ValueError):
        bitrate = 20_000_000
    return max(floor, min(ceiling, bitrate))


def has_audio_stream(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return bool(json.loads(out.stdout).get("streams"))


def extract_audio(path: Path, sample_rate: int, max_seconds: float) -> np.ndarray:
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-t", str(max_seconds),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)


def extract_audio_window(path: Path, start: float, duration: float, sample_rate: int) -> np.ndarray:
    """Extrai um trecho de áudio começando em `start` segundos (busca
    rápida+precisa: pula perto com -ss antes do -i e refina depois)."""
    fast_seek = max(0.0, start - 5.0)
    accurate_seek = start - fast_seek
    cmd = ["ffmpeg", "-v", "error"]
    if fast_seek > 0:
        cmd += ["-ss", f"{fast_seek:.3f}"]
    cmd += ["-i", str(path)]
    if accurate_seek > 0:
        cmd += ["-ss", f"{accurate_seek:.3f}"]
    cmd += ["-t", f"{duration:.3f}",
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)


def zscore(a: np.ndarray) -> np.ndarray:
    std = a.std()
    return (a - a.mean()) / std if std > 1e-6 else a - a.mean()


def phat_whiten(a: np.ndarray) -> np.ndarray:
    """'Branqueia' o sinal no domínio da frequência preservando a fase mas
    normalizando a magnitude (GCC-PHAT). Deixa o pico da correlação mais
    nítido e menos sensível a ruído/reverberação do que a correlação normal,
    sem depender de qual sinal é mais 'alto'."""
    n = len(a)
    spec = np.fft.rfft(a)
    mag = np.abs(spec)
    mag[mag < 1e-10] = 1e-10
    return np.fft.irfft(spec / mag, n=n)


def match_offset(ref_audio: np.ndarray, sig_audio: np.ndarray, sr: int):
    """Retorna (offset_seconds, confidence).
    offset_seconds = instante da referência (t=0) em que `sig` começou a
    gravar (positivo = começou depois). confidence é um score ~[0,1] que
    indica o quão forte foi o pico de correlação (quanto maior, mais
    confiável o casamento entre os dois áudios).

    O instante do pico (o offset) é localizado usando GCC-PHAT, que é mais
    robusto a ruído de fundo e eco/reverberação do que a correlação cruzada
    simples. A confiança continua sendo medida na correlação normal (não
    branqueada) nesse mesmo instante, para manter a mesma escala já
    calibrada nos limites de confiança usados pelo app."""
    r, s = zscore(ref_audio), zscore(sig_audio)

    rw, sw = phat_whiten(r), phat_whiten(s)
    corr_phat = correlate(sw, rw, mode="full", method="fft")
    lags = correlation_lags(len(sw), len(rw), mode="full")
    peak_idx = int(np.argmax(corr_phat))
    lag = lags[peak_idx]

    corr = correlate(s, r, mode="full", method="fft")
    confidence = float(corr[peak_idx] / min(len(r), len(s)))
    offset_seconds = -lag / sr
    return offset_seconds, confidence


def verify_offset_consistency(fa: Path, fb: Path, offset: float, sr: int,
                               window_seconds: float = 45.0, tolerance: float = 0.15):
    """Reamostra o áudio perto do meio e do fim da sobreposição entre os dois
    arquivos e recalcula o offset localmente nesses trechos. Se o offset
    encontrado nesses pontos divergir muito do offset original, é sinal de
    casamento errado (áudio parecido só por coincidência) ou deriva de
    relógio entre as câmeras ao longo da gravação.

    Retorna (consistent: bool, checks: list[(label, offset_recalculado, diferença, confiança)])."""
    dur_a = ffprobe_duration(fa)
    dur_b = ffprobe_duration(fb)
    overlap_start = max(0.0, offset)
    overlap_end = min(dur_a, offset + dur_b)
    overlap_duration = overlap_end - overlap_start

    checks = []
    if overlap_duration < 8.0:
        return True, checks

    win = max(2.0, min(window_seconds, overlap_duration * 0.3))
    checkpoints = []
    if overlap_duration >= win + 4:
        checkpoints.append(("meio", overlap_start + overlap_duration * 0.5 - win / 2))
    if overlap_duration >= win + 6:
        checkpoints.append(("fim", overlap_end - win - 2.0))

    consistent = True
    for label, t_fa in checkpoints:
        t_fa = max(overlap_start, t_fa)
        t_fb = t_fa - offset
        if t_fb < 0:
            continue
        try:
            wa = extract_audio_window(fa, t_fa, win, sr)
            wb = extract_audio_window(fb, t_fb, win, sr)
        except subprocess.CalledProcessError:
            continue
        if len(wa) < sr or len(wb) < sr:
            continue
        local_offset, local_conf = match_offset(wa, wb, sr)
        diff = abs(local_offset)
        checks.append((label, offset + local_offset, diff, local_conf))
        if diff > tolerance:
            consistent = False
    return consistent, checks


CLOCK_OFFSET_TOLERANCE = 20.0  # segundos de folga aceitos ao redor do deslocamento de relógio estimado
CLOCK_OFFSET_BUCKET = 5.0      # tamanho do "balde" (segundos) usado para agrupar deltas parecidos
CLOCK_OFFSET_MIN_VOTES = 2     # nº mínimo de pares concordando no mesmo deslocamento p/ confiar nele


def probe_recording_span(path: Path):
    """Retorna (inicio_epoch, fim_epoch) da gravação com base no metadado
    'creation_time' embutido no arquivo pela câmera, ou None se o arquivo não
    tiver esse metadado. Esse campo normalmente registra o instante em que a
    gravação foi FINALIZADA (quando o cabeçalho do arquivo é fechado), então
    o início é calculado como fim - duração."""
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration:format_tags=creation_time",
           "-of", "default=noprint_wrappers=1", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, text=True).stdout
    except subprocess.CalledProcessError:
        return None

    duration = None
    creation_time = None
    for line in out.splitlines():
        if line.startswith("duration="):
            try:
                duration = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("TAG:creation_time="):
            creation_time = line.split("=", 1)[1].strip()

    if duration is None or not creation_time:
        return None
    try:
        end = datetime.fromisoformat(creation_time.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return end - duration, end


def estimate_clock_offset(files_a, files_b, spans,
                           bucket=CLOCK_OFFSET_BUCKET, min_votes=CLOCK_OFFSET_MIN_VOTES):
    """As duas câmeras normalmente NÃO têm o relógio interno sincronizado
    entre si — mas cada uma mantém seu próprio relógio andando a um ritmo
    constante durante a gravação. Isso significa que, para os pares
    genuinamente corretos, a diferença (fim_A - fim_B) do metadado
    'creation_time' é praticamente a MESMA para todos eles (o deslocamento
    fixo entre os dois relógios), enquanto para pares errados essa diferença
    é essencialmente aleatória.

    Esta função testa todas as combinações A×B, agrupa as diferenças em
    'baldes' de poucos segundos e retorna o valor central do balde mais
    votado — ou None se nenhum balde tiver votos suficientes para confiar
    (ex.: poucos arquivos, ou câmeras sem correspondência real)."""
    deltas = []
    for fa in files_a:
        sa = spans.get(fa)
        if sa is None:
            continue
        for fb in files_b:
            sb = spans.get(fb)
            if sb is None:
                continue
            deltas.append(sa[1] - sb[1])

    if not deltas:
        return None
    votes = {}
    for d in deltas:
        votes.setdefault(round(d / bucket), []).append(d)
    best_bucket, best_deltas = max(votes.items(), key=lambda kv: len(kv[1]))
    if len(best_deltas) < min_votes:
        return None
    return sum(best_deltas) / len(best_deltas)


def _time_plausible(span_a, span_b, clock_offset, tolerance=CLOCK_OFFSET_TOLERANCE):
    """True se a diferença entre os horários de término dos dois arquivos
    bate com o deslocamento de relógio estimado entre as câmeras (dentro de
    `tolerance`). Se faltar metadado de algum lado, ou não houver um
    deslocamento confiável estimado, não filtra (cai no comportamento
    antigo, só por áudio)."""
    if span_a is None or span_b is None or clock_offset is None:
        return True
    delta = span_a[1] - span_b[1]
    return abs(delta - clock_offset) <= tolerance


def auto_pair(files_a, files_b, audio, sr):
    spans = {f: probe_recording_span(f) for f in files_a + files_b}
    clock_offset = estimate_clock_offset(files_a, files_b, spans)

    candidates = []
    for fa in files_a:
        for fb in files_b:
            if not _time_plausible(spans.get(fa), spans.get(fb), clock_offset):
                continue
            offset, conf = match_offset(audio[fa], audio[fb], sr)
            candidates.append((conf, fa, fb, offset))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_a, used_b, pairs = set(), set(), []
    for conf, fa, fb, offset in candidates:
        if fa in used_a or fb in used_b:
            continue
        pairs.append((fa, fb, offset, conf))
        used_a.add(fa)
        used_b.add(fb)

    pairs.sort(key=lambda p: p[0].name)
    unmatched_a = [f for f in files_a if f not in used_a]
    unmatched_b = [f for f in files_b if f not in used_b]
    return pairs, unmatched_a, unmatched_b


def probe_video_profile(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(out.stdout).get("streams") or [{}]
    s = streams[0]
    fps = None
    afr = s.get("avg_frame_rate")
    if afr and afr != "0/0":
        num, _, den = afr.partition("/")
        try:
            den_f = float(den) if den else 0.0
            fps = round(float(num) / den_f, 2) if den_f else None
        except ValueError:
            fps = None
    return {"width": s.get("width"), "height": s.get("height"), "fps": fps, "codec": s.get("codec_name")}


def _filename_prefix(name: str) -> str:
    return Path(name).stem.rstrip("0123456789")


def _split_by_number_gap(files):
    """Quando todos os arquivos têm o mesmo prefixo alfabético (ex:
    MVI_0553 e MVI_1990 — ambos 'MVI_'), tenta separar em dois grupos pelo
    salto na numeração final do nome (dois cartões/câmeras costumam ter
    faixas de número bem distantes uma da outra). Retorna [grupo1, grupo2]
    ou None se não houver um salto claramente maior que o passo típico."""
    numbered = []
    for f in files:
        m = re.search(r"(\d+)$", Path(f.name).stem)
        if not m:
            return None
        numbered.append((int(m.group(1)), f))
    if len(numbered) < 2:
        return None

    numbered.sort(key=lambda t: t[0])
    diffs = [numbered[i + 1][0] - numbered[i][0] for i in range(len(numbered) - 1)]
    gap_idx = max(range(len(diffs)), key=lambda i: diffs[i])
    gap = diffs[gap_idx]
    other_diffs = diffs[:gap_idx] + diffs[gap_idx + 1:]
    typical_step = statistics.median(other_diffs) if other_diffs else 1
    if gap <= max(3, typical_step * 3):
        return None

    group1 = [f for _, f in numbered[:gap_idx + 1]]
    group2 = [f for _, f in numbered[gap_idx + 1:]]
    return [group1, group2]


def auto_split_cameras(files):
    """Separa uma lista de arquivos (câmeras misturadas numa pasta só) em
    dois grupos automaticamente. Tenta, em ordem:
    1. Características do vídeo (resolução + fps) — tendem a ser estáveis
       por corpo de câmera, independente do nome do arquivo;
    2. Prefixo alfabético do nome do arquivo (ex: 'GOPR' vs 'MVI_');
    3. Salto na numeração do nome, quando o prefixo é igual para todos
       (ex: MVI_0553..0560 vs MVI_1990..1996).

    Retorna (grupo_a, grupo_b, metodo, perfis). Levanta ValueError se
    nenhum critério separar com confiança em exatamente dois grupos — nesse
    caso o chamador deve pedir para o usuário separar manualmente."""
    profiles = {f: probe_video_profile(f) for f in files}

    def profile_key(f):
        p = profiles[f]
        return (p["width"], p["height"], p["fps"])

    by_profile = {}
    for f in files:
        by_profile.setdefault(profile_key(f), []).append(f)

    if len(by_profile) == 2:
        groups = list(by_profile.values())
        method = "características do vídeo (resolução/fps)"
    else:
        by_prefix = {}
        for f in files:
            by_prefix.setdefault(_filename_prefix(f.name), []).append(f)

        if len(by_prefix) == 2:
            groups = list(by_prefix.values())
            method = "prefixo do nome do arquivo"
        else:
            gap_groups = _split_by_number_gap(files) if len(by_prefix) == 1 else None
            if gap_groups:
                groups = gap_groups
                method = "salto na numeração do nome do arquivo"
            else:
                raise ValueError(
                    "Não foi possível separar os vídeos automaticamente em duas câmeras "
                    f"({len(by_profile)} perfis de vídeo diferentes, {len(by_prefix)} prefixos de "
                    "nome diferentes, sem salto claro na numeração). Separe os arquivos manualmente "
                    "em duas pastas."
                )

    groups = [sorted(g, key=lambda f: f.name) for g in groups]
    groups.sort(key=lambda g: g[0].name)
    return groups[0], groups[1], method, profiles


def load_manual_pairs(path, by_name_a, by_name_b, audio, sr):
    pairs = []
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].strip().startswith("#"):
                continue
            a_name, b_name = row[0].strip(), row[1].strip()
            if a_name not in by_name_a or b_name not in by_name_b:
                sys.exit(f"Par manual inválido: '{a_name}' / '{b_name}' não encontrado nas pastas.")
            fa, fb = by_name_a[a_name], by_name_b[b_name]
            offset, conf = match_offset(audio[fa], audio[fb], sr)
            pairs.append((fa, fb, offset, conf))
    return pairs


def run_trim(src: Path, dst: Path, trim_seconds: float, duration: float,
             copy_mode: bool, hw_encode: bool, cancel_event=None):
    fast_seek = max(0.0, trim_seconds - 5.0)
    accurate_seek = trim_seconds - fast_seek

    cmd = ["ffmpeg", "-y", "-v", "error", "-stats"]
    if fast_seek > 0:
        cmd += ["-ss", f"{fast_seek:.3f}"]
    cmd += ["-i", str(src)]
    if accurate_seek > 0:
        cmd += ["-ss", f"{accurate_seek:.3f}"]
    cmd += ["-t", f"{duration:.3f}"]

    if copy_mode:
        cmd += ["-c", "copy"]
    elif hw_encode:
        bitrate = source_bitrate(src)
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", str(bitrate),
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(dst)]

    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()

    if cancel_event is None:
        subprocess.run(cmd, check=True)
        return

    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        if cancel_event.is_set():
            proc.terminate()
            proc.wait()
            dst.unlink(missing_ok=True)
            raise Cancelled()
        time.sleep(0.3)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def sync_pair(fa: Path, fb: Path, offset: float, out_dir: Path, index: int,
              copy_mode: bool, equal_length: bool, hw_encode: bool, cancel_event=None):
    dur_a, dur_b = ffprobe_duration(fa), ffprobe_duration(fb)
    start_a, start_b = 0.0, offset
    common_start = max(start_a, start_b)
    trim_a = common_start - start_a
    trim_b = common_start - start_b

    if equal_length:
        out_dur = min(dur_a - trim_a, dur_b - trim_b)
        out_dur_a = out_dur_b = out_dur
    else:
        out_dur_a = dur_a - trim_a
        out_dur_b = dur_b - trim_b

    tag = f"take{index:02d}"
    dst_a = out_dir / f"{tag}_{fa.stem}{fa.suffix}"
    dst_b = out_dir / f"{tag}_{fb.stem}{fb.suffix}"
    run_trim(fa, dst_a, trim_a, out_dur_a, copy_mode, hw_encode, cancel_event)
    run_trim(fb, dst_b, trim_b, out_dur_b, copy_mode, hw_encode, cancel_event)
    return dst_a, dst_b, trim_a, trim_b, out_dur_a, out_dur_b


def run(args, output_dir):
    hw_encode = platform.system() == "Darwin" and not args.software_encoder and not args.copy

    files_a = list_video_files(args.cam_a_dir)
    files_b = list_video_files(args.cam_b_dir)
    for f in files_a + files_b:
        if not has_audio_stream(f):
            sys.exit(f"O arquivo {f.name} não tem trilha de áudio — não é possível sincronizar por áudio.")

    print(f"Câmera A: {len(files_a)} arquivo(s) em {args.cam_a_dir}")
    print(f"Câmera B: {len(files_b)} arquivo(s) em {args.cam_b_dir}")
    print("Extraindo e analisando áudio...")

    all_files = files_a + files_b
    audio = {}
    for idx, f in enumerate(all_files, start=1):
        print(f"  - {f.name}")
        if not args.dry_run:
            write_progress(output_dir, f"Analisando áudio: {idx}/{len(all_files)} arquivo(s)")
        audio[f] = extract_audio(f, args.sample_rate, args.analyze_seconds)

    if args.pairs:
        by_name_a = {f.name: f for f in files_a}
        by_name_b = {f.name: f for f in files_b}
        pairs = load_manual_pairs(args.pairs, by_name_a, by_name_b, audio, args.sample_rate)
        matched_a = {p[0] for p in pairs}
        matched_b = {p[1] for p in pairs}
        unmatched_a = [f for f in files_a if f not in matched_a]
        unmatched_b = [f for f in files_b if f not in matched_b]
    else:
        pairs, unmatched_a, unmatched_b = auto_pair(files_a, files_b, audio, args.sample_rate)

    print("\nPareamento encontrado:")
    good_pairs, low_pairs = [], []
    for fa, fb, offset, conf in pairs:
        ok = conf >= args.min_confidence
        (good_pairs if ok else low_pairs).append((fa, fb, offset, conf))
        flag = "" if ok else "  <-- confiança baixa, será PULADO (use --pairs ou --min-confidence para forçar)"
        print(f"  {fa.name:30s} <-> {fb.name:30s}  offset: {offset:+8.3f}s  confiança: {conf:.3f}{flag}")

    if unmatched_a:
        print("\nArquivos da câmera A sem par encontrado:")
        for f in unmatched_a:
            print(f"  - {f.name}")
    if unmatched_b:
        print("\nArquivos da câmera B sem par encontrado:")
        for f in unmatched_b:
            print(f"  - {f.name}")

    if not good_pairs:
        sys.exit("\nNenhum par com confiança suficiente para sincronizar.")

    if args.dry_run:
        print("\n(dry-run: nenhum vídeo foi gerado)")
        return

    report_path = output_dir / "pairing_report.csv"
    with open(report_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["cam_a_file", "cam_b_file", "offset_seconds", "confidence", "status"])
        for fa, fb, offset, conf in good_pairs:
            writer.writerow([fa.name, fb.name, f"{offset:.3f}", f"{conf:.3f}", "synced"])
        for fa, fb, offset, conf in low_pairs:
            writer.writerow([fa.name, fb.name, f"{offset:.3f}", f"{conf:.3f}", "skipped_low_confidence"])

    print("\nGerando vídeos sincronizados...")
    write_progress(output_dir, f"Sincronizando: 0/{len(good_pairs)} pares concluídos")
    for i, (fa, fb, offset, conf) in enumerate(good_pairs, start=1):
        dst_a, dst_b, trim_a, trim_b, dur_a, dur_b = sync_pair(
            fa, fb, offset, output_dir, i, args.copy, not args.no_equal_length, hw_encode
        )
        print(f"  take{i:02d}: {dst_a.name} (corte {trim_a:.3f}s) + {dst_b.name} (corte {trim_b:.3f}s), duração {dur_a:.3f}s")
        write_progress(output_dir, f"Sincronizando: {i}/{len(good_pairs)} pares concluídos")

    print(f"\nPronto! Vídeos sincronizados em: {output_dir}")
    print(f"Relatório de pareamento salvo em: {report_path}")
    write_progress(output_dir, f"Concluído: {len(good_pairs)}/{len(good_pairs)} pares")
    (output_dir / "DONE").write_text("ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cam_a_dir", type=Path, help="Pasta com os vídeos da câmera A")
    ap.add_argument("cam_b_dir", type=Path, help="Pasta com os vídeos da câmera B")
    ap.add_argument("output_dir", type=Path, help="Pasta onde salvar os vídeos sincronizados")
    ap.add_argument("--pairs", type=Path, default=None,
                     help="CSV manual (camA_arquivo.mp4,camB_arquivo.mp4 por linha) para forçar o pareamento em vez do automático")
    ap.add_argument("--sample-rate", type=int, default=8000,
                     help="Sample rate usado na análise de áudio (padrão: 8000)")
    ap.add_argument("--analyze-seconds", type=float, default=180.0,
                     help="Quantos segundos do início de cada vídeo analisar (padrão: 180)")
    ap.add_argument("--min-confidence", type=float, default=0.08,
                     help="Confiança mínima do casamento de áudio para sincronizar automaticamente (padrão: 0.08)")
    ap.add_argument("--copy", action="store_true",
                     help="Corta com stream copy (mais rápido, mas o corte pode 'grudar' no keyframe mais próximo)")
    ap.add_argument("--software-encoder", action="store_true",
                     help="Usa libx264 (CPU) em vez do codificador de hardware do Mac. Mais lento (~2x), arquivos um pouco menores.")
    ap.add_argument("--no-equal-length", action="store_true",
                     help="Não corta o final dos pares para ficarem com a mesma duração")
    ap.add_argument("--dry-run", action="store_true",
                     help="Só calcula e mostra o pareamento/offsets, sem gerar os vídeos")
    args = ap.parse_args()

    check_ffmpeg()

    output_dir = args.output_dir
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    try:
        run(args, output_dir)
    except SystemExit as e:
        if not args.dry_run:
            (output_dir / "FAILED").write_text(str(e.code) if e.code else "erro desconhecido")
            write_progress(output_dir, "Falhou — veja o log")
        raise
    except Exception as e:
        if not args.dry_run:
            (output_dir / "FAILED").write_text(str(e))
            write_progress(output_dir, "Falhou — veja o log")
        raise


if __name__ == "__main__":
    main()
