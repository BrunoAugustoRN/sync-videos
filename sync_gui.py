#!/usr/bin/env python3
"""
Interface gráfica mínima para o sync_videos.py: escolha as pastas das
duas câmeras (ou uma pasta única com as duas misturadas), confirme o
pareamento detectado e clique em Sincronizar — acompanhe o progresso e os
avisos numa janela, sem precisar de terminal.

Requisitos: os mesmos do sync_videos.py (ffmpeg/ffprobe no PATH,
numpy/scipy instalados) mais tkinter (já vem com o Python do macOS).
"""

import csv
import platform
import queue
import statistics
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sync_videos as sv

MIN_CONFIDENCE = 0.08
REVIEW_CONFIDENCE = 0.15
SAMPLE_RATE = 8000
ANALYZE_SECONDS = 180.0

BG = "#262b33"
BG_PANEL = "#2f3540"
BG_FIELD = "#20242b"
BORDER = "#454c58"
FG = "#dfe3ea"
FG_MUTED = "#8f97a3"
ACCENT = "#6f9bd1"
ACCENT_FG = "#12161c"
COLOR_OK = "#5cc99a"
COLOR_WARN = "#e0b96a"
COLOR_ERROR = "#e17b7b"
COLOR_INFO = "#8f97a3"
WATERMARK_TEXT = "Sincronizador"
UI_FONT = "Nunito"
WATERMARK_FONT = "Arial Rounded MT Bold"


class RoundedButton(tk.Canvas):
    """Botão com cantos arredondados desenhado no Canvas — o tema 'clam' do
    ttk não tem como fazer border-radius. Aceita .configure(state=...) igual
    a um ttk.Button para plugar sem mudar o resto do código."""

    def __init__(self, parent, text, command=None, accent=False,
                 height=36, radius=14, font=None, bg=BG_PANEL, padx=22):
        self._font = font or (UI_FONT, 11, "bold")
        f = tkfont.Font(family=self._font[0], size=self._font[1],
                         weight=self._font[2] if len(self._font) > 2 else "normal")
        width = max(96, f.measure(text) + padx * 2)
        super().__init__(parent, width=width, height=height, bg=bg,
                          highlightthickness=0, bd=0, cursor="hand2")
        self._command = command
        self._radius = radius
        self._text = text
        self._state = "normal"
        if accent:
            self._fill, self._fill_hover, self._fill_press = ACCENT, "#8bb0dd", "#5580b3"
            self._fill_disabled, self._fg, self._fg_disabled = "#39485a", ACCENT_FG, "#6b7686"
        else:
            self._fill, self._fill_hover, self._fill_press = "#3a4048", "#454c56", "#30353d"
            self._fill_disabled, self._fg, self._fg_disabled = "#282c33", FG, "#5f6772"
        self._current_fill = self._fill
        self._redraw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    @staticmethod
    def _round_rect_points(x1, y1, x2, y2, r):
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

    def _redraw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        disabled = self._state == "disabled"
        fill = self._fill_disabled if disabled else self._current_fill
        fg = self._fg_disabled if disabled else self._fg
        self.create_polygon(self._round_rect_points(1, 1, w - 1, h - 1, self._radius),
                             fill=fill, outline=fill, smooth=True)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e):
        if self._state != "disabled":
            self._current_fill = self._fill_hover
            self._redraw()

    def _on_leave(self, _e):
        self._current_fill = self._fill
        self._redraw()

    def _on_press(self, _e):
        if self._state != "disabled":
            self._current_fill = self._fill_press
            self._redraw()

    def _on_release(self, e):
        if self._state == "disabled":
            return
        self._current_fill = self._fill_hover
        self._redraw()
        if 0 <= e.x <= int(self["width"]) and 0 <= e.y <= int(self["height"]) and self._command:
            self._command()

    def configure(self, **kwargs):
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            self._redraw()
        if kwargs:
            super().configure(**kwargs)

    config = configure


def flag_offset_outliers(good_pairs, min_group=3, mad_multiplier=3.0, floor_seconds=0.5):
    """Marca pares cujo offset foge muito do padrão do restante do lote —
    sinal de possível casamento incorreto mesmo com confiança aceitável."""
    if len(good_pairs) < min_group:
        return [False] * len(good_pairs)
    offsets = [p[2] for p in good_pairs]
    med = statistics.median(offsets)
    mad = statistics.median(abs(o - med) for o in offsets)
    threshold = max(floor_seconds, mad_multiplier * mad)
    return [abs(o - med) > threshold for o in offsets]


class SyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sync Videos")
        self.geometry("780x700")
        self.minsize(680, 560)
        self.configure(bg=BG)

        self.mode = tk.StringVar(value="split")  # "split" = 2 pastas, "mixed" = 1 pasta
        self.cam_a = tk.StringVar()
        self.cam_b = tk.StringVar()
        self.mixed_dir = tk.StringVar()
        self.out_dir = tk.StringVar()
        self.status = tk.StringVar(value="Escolha as pastas das duas câmeras.")

        self.work_queue = queue.Queue()
        self.worker_thread = None
        self.cancel_event = threading.Event()
        self.last_output_dir = None
        self.running = False

        self._apply_dark_theme()
        self._build_canvas()
        self._build_ui()
        self._on_mode_change()
        self.after(150, self._poll_queue)

    # ---------- tema / fundo ----------

    def _apply_dark_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG_PANEL, foreground=FG,
                         fieldbackground=BG_FIELD, bordercolor=BORDER,
                         lightcolor=BG_PANEL, darkcolor=BG_PANEL, font=(UI_FONT, 11))
        style.configure("TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG_PANEL, foreground=FG)
        style.configure("TRadiobutton", background=BG_PANEL, foreground=FG)
        style.map("TRadiobutton", background=[("active", BG_PANEL)])

        style.configure("TEntry", fieldbackground=BG_FIELD, foreground=FG,
                         insertcolor=FG, bordercolor=BORDER, padding=5)
        style.configure("TProgressbar", background=ACCENT, troughcolor=BG_FIELD, bordercolor=BORDER)
        style.configure("TScrollbar", background="#2b2b2b", troughcolor=BG_FIELD,
                         bordercolor=BORDER, arrowcolor=FG)

        style.configure("Treeview", background=BG_FIELD, fieldbackground=BG_FIELD,
                         foreground=FG, bordercolor=BORDER, rowheight=26, font=(UI_FONT, 10))
        style.configure("Treeview.Heading", background="#2b2b2b", foreground=FG,
                         bordercolor=BORDER, font=(UI_FONT, 10, "bold"))
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", ACCENT_FG)])

    def _mk_listbox(self, parent, **kwargs):
        return tk.Listbox(parent, bg=BG_FIELD, fg=FG, selectbackground=ACCENT,
                           selectforeground=ACCENT_FG, highlightthickness=0,
                           relief="flat", bd=0, **kwargs)

    def _build_canvas(self):
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=6)
        self.canvas_window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self._margin = 34
        self.canvas.bind("<Configure>", self._on_canvas_resize)

    def _on_canvas_resize(self, event):
        m = self._margin
        w = max(1, event.width - 2 * m)
        h = max(1, event.height - 2 * m)
        self.canvas.coords(self.canvas_window, m, m)
        self.canvas.itemconfigure(self.canvas_window, width=w, height=h)
        self._draw_watermark(event.width, event.height)

    def _draw_watermark(self, width, height):
        """Marca d'água discreta no canto inferior direito, na moldura escura
        ao redor do painel — como uma sombra sutil no fundo."""
        self.canvas.delete("watermark")
        pad = 10
        self.canvas.create_text(width - pad, height - pad, text=WATERMARK_TEXT,
                                 fill="#343b46", font=(WATERMARK_FONT, 14),
                                 anchor="se", tags="watermark")
        self.canvas.tag_lower("watermark")

    # ---------- UI ----------

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        root = self.content

        mode_frm = ttk.Frame(root)
        mode_frm.pack(fill="x", **pad)
        ttk.Label(mode_frm, text="Vídeos:").pack(side="left")
        ttk.Radiobutton(mode_frm, text="Já separados em 2 pastas", value="split",
                         variable=self.mode, command=self._on_mode_change).pack(side="left", padx=(10, 0))
        ttk.Radiobutton(mode_frm, text="Pasta única (detectar câmeras automaticamente)", value="mixed",
                         variable=self.mode, command=self._on_mode_change).pack(side="left", padx=(10, 0))

        self.split_frm = ttk.Frame(root)
        self.split_frm.pack(fill="x", **pad)
        self.split_frm.columnconfigure(1, weight=1)
        self._folder_row(self.split_frm, "Câmera A:", self.cam_a, self._pick_cam_a, 0)
        self._folder_row(self.split_frm, "Câmera B:", self.cam_b, self._pick_cam_b, 1)

        self.mixed_frm = ttk.Frame(root)
        self.mixed_frm.pack(fill="x", **pad)
        self.mixed_frm.columnconfigure(1, weight=1)
        self._folder_row(self.mixed_frm, "Pasta com vídeos:", self.mixed_dir, self._pick_mixed, 0)

        self.out_frm = ttk.Frame(root)
        self.out_frm.pack(fill="x", **pad)
        self.out_frm.columnconfigure(1, weight=1)
        self._folder_row(self.out_frm, "Pasta de saída:", self.out_dir, self._pick_out, 0)

        btn_row = ttk.Frame(root)
        btn_row.pack(pady=(4, 10))
        self.sync_btn = RoundedButton(btn_row, "Sincronizar", accent=True, command=self._start_sync)
        self.sync_btn.pack(side="left", padx=4)
        self.cancel_btn = RoundedButton(btn_row, "Cancelar", command=self._cancel_sync)
        self.cancel_btn.configure(state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", padx=10)

        ttk.Label(root, textvariable=self.status, foreground=FG_MUTED).pack(anchor="w", padx=10, pady=(4, 10))

        ttk.Label(root, text="Avisos:").pack(anchor="w", padx=10)
        list_frame = ttk.Frame(root)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.warn_box = self._mk_listbox(list_frame, height=12, yscrollcommand=scrollbar.set)
        self.warn_box.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.warn_box.yview)

        bottom_row = ttk.Frame(root)
        bottom_row.pack(fill="x", padx=10, pady=(0, 10))
        self.open_btn = RoundedButton(bottom_row, "Abrir pasta de saída", command=self._open_output)
        self.open_btn.configure(state="disabled")
        self.open_btn.pack(side="right")

    def _on_mode_change(self):
        if self.mode.get() == "split":
            self.mixed_frm.pack_forget()
            self.split_frm.pack(fill="x", padx=10, pady=6, before=self.out_frm)
        else:
            self.split_frm.pack_forget()
            self.mixed_frm.pack(fill="x", padx=10, pady=6, before=self.out_frm)

    def _folder_row(self, parent, label, var, cmd, row):
        ttk.Label(parent, text=label, width=16).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)
        RoundedButton(parent, "Escolher…", height=30, command=cmd).grid(row=row, column=2, padx=(6, 0))

    def _pick_cam_a(self):
        d = filedialog.askdirectory(title="Escolha a pasta da câmera A")
        if d:
            self.cam_a.set(d)
            self._suggest_output(d)

    def _pick_cam_b(self):
        d = filedialog.askdirectory(title="Escolha a pasta da câmera B")
        if d:
            self.cam_b.set(d)

    def _pick_mixed(self):
        d = filedialog.askdirectory(title="Escolha a pasta com os vídeos das duas câmeras")
        if d:
            self.mixed_dir.set(d)
            self._suggest_output(d)

    def _pick_out(self):
        d = filedialog.askdirectory(title="Escolha a pasta de saída")
        if d:
            self.out_dir.set(d)

    def _suggest_output(self, base_dir):
        if self.out_dir.get():
            return
        base = Path(base_dir).parent
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_dir.set(str(base / f"sincronizado_{stamp}"))

    # ---------- controle da sincronização ----------

    def _start_sync(self):
        mixed_mode = self.mode.get() == "mixed"
        if mixed_mode:
            if not self.mixed_dir.get():
                messagebox.showwarning("Falta a pasta", "Escolha a pasta com os vídeos das duas câmeras.")
                return
            self._suggest_output(self.mixed_dir.get())
        else:
            if not self.cam_a.get() or not self.cam_b.get():
                messagebox.showwarning("Faltam pastas", "Escolha as pastas das duas câmeras antes de sincronizar.")
                return
            self._suggest_output(self.cam_a.get())

        self.warn_box.delete(0, "end")
        self.progress["value"] = 0
        self.sync_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.cancel_event.clear()
        self.running = True
        self.status.set("Iniciando…")

        output_dir = Path(self.out_dir.get())
        if mixed_mode:
            args = ("mixed", Path(self.mixed_dir.get()), output_dir)
        else:
            args = ("split", Path(self.cam_a.get()), Path(self.cam_b.get()), output_dir)

        self.worker_thread = threading.Thread(target=self._worker, args=args, daemon=True)
        self.worker_thread.start()

    def _cancel_sync(self):
        if self.running:
            self.cancel_event.set()
            self.status.set("Cancelando…")
            self.cancel_btn.configure(state="disabled")

    def _finish_run(self):
        self.running = False
        self.sync_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    # ---------- diálogos modais (rodam na thread principal) ----------

    def _ask_confirm(self, builder):
        """Mostra um diálogo modal construído por `builder(dialog)` e
        retorna True/False conforme o botão escolhido pelo usuário."""
        result = {"ok": False}
        dlg = tk.Toplevel(self)
        dlg.configure(bg=BG_PANEL)
        dlg.transient(self)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        builder(dlg, result)
        self.wait_window(dlg)
        return result["ok"]

    def _confirm_split(self, group_a, group_b, method):
        def builder(dlg, result):
            dlg.title("Confirmar separação por câmera")
            dlg.geometry("560x480")
            dlg.minsize(440, 320)
            dlg.resizable(True, True)

            # botões primeiro e fixados embaixo, para nunca ficarem
            # empurrados para fora da janela por conteúdo grande acima.
            btns = ttk.Frame(dlg)
            btns.pack(side="bottom", pady=12)

            def confirm():
                result["ok"] = True
                dlg.destroy()

            def cancel():
                result["ok"] = False
                dlg.destroy()

            RoundedButton(btns, "Confirmar e continuar", accent=True, command=confirm).pack(side="left", padx=6)
            RoundedButton(btns, "Cancelar", command=cancel).pack(side="left", padx=6)

            ttk.Label(dlg, text="Se a separação estiver errada, cancele e escolha "
                                 "'Já separados em 2 pastas' organizando os arquivos manualmente.",
                      wraplength=530, foreground=COLOR_WARN).pack(side="bottom", anchor="w", padx=12, pady=(4, 8))

            ttk.Label(dlg, text=f"Separação detectada por: {method}", font=(UI_FONT, 11, "bold")).pack(
                side="top", anchor="w", padx=12, pady=(12, 4))
            cols = ttk.Frame(dlg)
            cols.pack(side="top", fill="both", expand=True, padx=12, pady=6)
            cols.columnconfigure(0, weight=1)
            cols.columnconfigure(1, weight=1)

            for col, (title, files) in enumerate([
                (f"Câmera A ({len(group_a)} arquivo(s))", group_a),
                (f"Câmera B ({len(group_b)} arquivo(s))", group_b),
            ]):
                frm = ttk.Frame(cols)
                frm.grid(row=0, column=col, sticky="nsew", padx=4)
                ttk.Label(frm, text=title).pack(anchor="w")
                lb = self._mk_listbox(frm, height=14)
                lb.pack(fill="both", expand=True)
                for f in files:
                    lb.insert("end", f.name)

        return self._ask_confirm(builder)

    def _confirm_pairing(self, good_pairs, low_pairs, unmatched_a, unmatched_b, review_reason):
        def builder(dlg, result):
            dlg.title("Confirmar pareamento antes de gerar os vídeos")
            dlg.geometry("680x520")
            dlg.minsize(520, 360)
            dlg.resizable(True, True)

            # botões primeiro e fixados embaixo, para nunca ficarem
            # empurrados para fora da janela por conteúdo grande acima.
            btns = ttk.Frame(dlg)
            btns.pack(side="bottom", pady=12)

            def confirm():
                result["ok"] = True
                dlg.destroy()

            def cancel():
                result["ok"] = False
                dlg.destroy()

            RoundedButton(btns, "Confirmar e gerar vídeos", accent=True, command=confirm).pack(side="left", padx=6)
            RoundedButton(btns, "Cancelar", command=cancel).pack(side="left", padx=6)

            extra = []
            if unmatched_a:
                extra.append("Sem par (câmera A): " + ", ".join(f.name for f in unmatched_a))
            if unmatched_b:
                extra.append("Sem par (câmera B): " + ", ".join(f.name for f in unmatched_b))
            if extra:
                extra_frm = ttk.Frame(dlg)
                extra_frm.pack(side="bottom", fill="x", padx=12, pady=(0, 4))
                extra_lb = self._mk_listbox(extra_frm, height=3)
                extra_lb.pack(fill="x")
                for line in extra:
                    extra_lb.insert("end", line)

            ttk.Label(dlg, text="Pares detectados — confira antes de codificar:",
                      font=(UI_FONT, 11, "bold")).pack(side="top", anchor="w", padx=12, pady=(12, 4))

            cols = ("cam_a", "cam_b", "offset", "confianca", "status")
            tree = ttk.Treeview(dlg, columns=cols, show="headings", height=12)
            headings = {"cam_a": "Câmera A", "cam_b": "Câmera B", "offset": "Offset (s)",
                        "confianca": "Confiança", "status": "Status"}
            widths = {"cam_a": 170, "cam_b": 170, "offset": 80, "confianca": 80, "status": 150}
            for c in cols:
                tree.heading(c, text=headings[c])
                tree.column(c, width=widths[c], anchor="w")
            tree.pack(side="top", fill="both", expand=True, padx=12, pady=6)

            for fa, fb, offset, conf in good_pairs:
                reason = review_reason.get((fa, fb))
                status = "OK" if not reason else f"CONFIRA: {reason}"
                tag = "warn" if reason else "ok"
                tree.insert("", "end", values=(fa.name, fb.name, f"{offset:+.3f}", f"{conf:.3f}", status), tags=(tag,))
            for fa, fb, offset, conf in low_pairs:
                tree.insert("", "end", values=(fa.name, fb.name, f"{offset:+.3f}", f"{conf:.3f}",
                                                "será PULADO (confiança baixa)"), tags=("bad",))
            tree.tag_configure("warn", foreground=COLOR_WARN)
            tree.tag_configure("bad", foreground=COLOR_ERROR)
            tree.tag_configure("ok", foreground=COLOR_OK)

        return self._ask_confirm(builder)

    # ---------- worker (thread em segundo plano) ----------

    def _worker(self, mode, *rest):
        q = self.work_queue
        warning_lines = []
        output_dir = rest[-1]
        try:
            sv.check_ffmpeg()

            if mode == "mixed":
                mixed_dir = rest[0]
                all_in = sv.list_video_files(mixed_dir)
                q.put(("progress", 0, 1, f"Detectando câmeras em {len(all_in)} arquivo(s)…"))
                try:
                    group_a, group_b, method, _profiles = sv.auto_split_cameras(all_in)
                except ValueError as e:
                    q.put(("error", str(e)))
                    return

                holder = {}
                event = threading.Event()
                q.put(("split_preview", group_a, group_b, method, holder, event))
                event.wait()
                if not holder.get("ok"):
                    q.put(("cancelled",))
                    return
                files_a, files_b = group_a, group_b
            else:
                cam_a_dir, cam_b_dir = rest[0], rest[1]
                files_a = sv.list_video_files(cam_a_dir)
                files_b = sv.list_video_files(cam_b_dir)

            self._check_cancel()
            output_dir.mkdir(parents=True, exist_ok=True)

            for f in files_a + files_b:
                if not sv.has_audio_stream(f):
                    raise RuntimeError(
                        f"{f.name} não tem trilha de áudio — não é possível sincronizar por áudio."
                    )

            all_files = files_a + files_b
            audio = {}
            for i, f in enumerate(all_files, start=1):
                self._check_cancel()
                q.put(("progress", i, len(all_files), f"Analisando áudio: {i}/{len(all_files)} — {f.name}"))
                audio[f] = sv.extract_audio(f, SAMPLE_RATE, ANALYZE_SECONDS)

            pairs, unmatched_a, unmatched_b = sv.auto_pair(files_a, files_b, audio, SAMPLE_RATE)

            for f in unmatched_a:
                line = f"Sem par encontrado (câmera A): {f.name}"
                warning_lines.append(("info", line))
                q.put(("warn", "info", line))
            for f in unmatched_b:
                line = f"Sem par encontrado (câmera B): {f.name}"
                warning_lines.append(("info", line))
                q.put(("warn", "info", line))

            # A confiança pura de áudio pode ser um sinal fraco (ruído de
            # fundo diferente entre as câmeras). Por isso, sempre que o vídeo
            # for longo o bastante, a checagem de consistência multi-janela
            # (mais confiável, porque reamostra o áudio de novo em outros
            # pontos do vídeo) decide se o par é aceito — a confiança de
            # áudio só desempata quando o vídeo é curto demais para verificar.
            good_pairs, low_pairs = [], []
            review_reason = {}
            for i, (fa, fb, offset, conf) in enumerate(pairs, start=1):
                self._check_cancel()
                key = (fa, fb)
                q.put(("progress", i, len(pairs), f"Verificando consistência: {i}/{len(pairs)}"))
                consistent, checks = sv.verify_offset_consistency(fa, fb, offset, SAMPLE_RATE)

                if checks:
                    good_pairs.append((fa, fb, offset, conf))
                    if not consistent:
                        detail = "; ".join(f"{label} diff {diff:.2f}s" for label, _o, diff, _c in checks)
                        line = (f"CONFIRA: offset não é consistente ao longo do vídeo ({detail}) — "
                                f"possível casamento errado ou deriva de relógio entre as câmeras: "
                                f"{fa.name} <-> {fb.name}")
                        warning_lines.append(("warn", line))
                        q.put(("warn", "warn", line))
                        review_reason[key] = "offset inconsistente"
                    elif conf < REVIEW_CONFIDENCE:
                        line = (f"Confiança de áudio baixa ({conf:.2f}), mas offset confirmado ao "
                                f"longo do vídeo: {fa.name} <-> {fb.name}")
                        warning_lines.append(("info", line))
                        q.put(("warn", "info", line))
                elif conf >= MIN_CONFIDENCE:
                    good_pairs.append((fa, fb, offset, conf))
                    if conf < REVIEW_CONFIDENCE:
                        line = (f"Confiança baixa ({conf:.2f}) e vídeo curto demais para "
                                f"verificar de outro jeito, vale conferir: {fa.name} <-> {fb.name}")
                        warning_lines.append(("warn", line))
                        q.put(("warn", "warn", line))
                        review_reason[key] = "confiança baixa"
                else:
                    low_pairs.append((fa, fb, offset, conf))
                    line = (f"NÃO sincronizado (confiança muito baixa {conf:.2f}, e vídeo curto "
                            f"demais para verificar de outro jeito): {fa.name} <-> {fb.name}")
                    warning_lines.append(("error", line))
                    q.put(("warn", "error", line))

            if not good_pairs:
                raise RuntimeError("Nenhum par com confiança suficiente para sincronizar.")

            outlier_flags = flag_offset_outliers(good_pairs)
            for (fa, fb, offset, conf), is_outlier in zip(good_pairs, outlier_flags):
                key = (fa, fb)
                if is_outlier and key not in review_reason:
                    line = (f"CONFIRA: offset incomum ({offset:+.3f}s foge do padrão dos outros pares): "
                            f"{fa.name} <-> {fb.name}")
                    warning_lines.append(("warn", line))
                    q.put(("warn", "warn", line))
                    review_reason[key] = "offset incomum"

            holder = {}
            event = threading.Event()
            q.put(("pairing_preview", good_pairs, low_pairs, unmatched_a, unmatched_b, review_reason, holder, event))
            event.wait()
            if not holder.get("ok"):
                q.put(("cancelled",))
                return

            hw_encode = platform.system() == "Darwin"
            total = len(good_pairs)
            q.put(("progress", 0, total, f"Sincronizando: 0/{total} pares concluídos"))
            for i, (fa, fb, offset, conf) in enumerate(good_pairs, start=1):
                sv.sync_pair(fa, fb, offset, output_dir, i, False, True, hw_encode, self.cancel_event)
                q.put(("progress", i, total, f"Sincronizando: {i}/{total} pares concluídos"))

            self._write_reports(output_dir, good_pairs, low_pairs, review_reason, warning_lines)
            q.put(("done", str(output_dir)))
        except sv.Cancelled:
            q.put(("cancelled",))
        except Exception as e:
            try:
                (output_dir / "avisos.txt").write_text(
                    "\n".join(f"[{lvl}] {ln}" for lvl, ln in warning_lines) + f"\n[erro] {e}\n"
                )
            except OSError:
                pass
            q.put(("error", str(e)))

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise sv.Cancelled()

    def _write_reports(self, output_dir, good_pairs, low_pairs, review_reason, warning_lines):
        with open(output_dir / "pairing_report.csv", "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["cam_a_file", "cam_b_file", "offset_seconds", "confidence", "status"])
            for fa, fb, offset, conf in good_pairs:
                reason = review_reason.get((fa, fb))
                status = "synced" if not reason else f"synced_review_{reason.replace(' ', '_')}"
                writer.writerow([fa.name, fb.name, f"{offset:.3f}", f"{conf:.3f}", status])
            for fa, fb, offset, conf in low_pairs:
                writer.writerow([fa.name, fb.name, f"{offset:.3f}", f"{conf:.3f}", "skipped_low_confidence"])

        if warning_lines:
            (output_dir / "avisos.txt").write_text(
                "\n".join(f"[{lvl}] {ln}" for lvl, ln in warning_lines) + "\n"
            )

    # ---------- fila de eventos (thread principal) ----------

    def _poll_queue(self):
        try:
            while True:
                item = self.work_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, text = item
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.status.set(text)
                elif kind == "warn":
                    _, level, text = item
                    self.warn_box.insert("end", text)
                    idx = self.warn_box.size() - 1
                    color = {"error": COLOR_ERROR, "warn": COLOR_WARN, "info": FG_MUTED}.get(level, FG)
                    self.warn_box.itemconfig(idx, fg=color)
                elif kind == "split_preview":
                    _, group_a, group_b, method, holder, event = item
                    holder["ok"] = self._confirm_split(group_a, group_b, method)
                    event.set()
                elif kind == "pairing_preview":
                    _, good_pairs, low_pairs, unmatched_a, unmatched_b, review_reason, holder, event = item
                    holder["ok"] = self._confirm_pairing(good_pairs, low_pairs, unmatched_a, unmatched_b, review_reason)
                    event.set()
                elif kind == "cancelled":
                    self.status.set("Cancelado.")
                    self._finish_run()
                elif kind == "done":
                    _, out_dir = item
                    self.last_output_dir = out_dir
                    self.status.set("Concluído!")
                    self._finish_run()
                    self.open_btn.configure(state="normal")
                    if self.warn_box.size() == 0:
                        self.warn_box.insert("end", "Nenhum aviso — tudo sincronizado com boa confiança.")
                    messagebox.showinfo("Sync Videos", "Vídeos sincronizados com sucesso!\n\n" + out_dir)
                elif kind == "error":
                    _, msg = item
                    self.status.set("Erro.")
                    self._finish_run()
                    messagebox.showerror("Sync Videos", "Deu erro:\n\n" + msg)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _open_output(self):
        if self.last_output_dir:
            subprocess.run(["open", self.last_output_dir])


if __name__ == "__main__":
    app = SyncApp()
    app.mainloop()
