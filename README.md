# Sync Videos

Sincroniza automaticamente as tomadas de duas câmeras usando o áudio gravado por elas.

Você aponta as pastas com os vídeos de cada câmera. O programa descobre qual arquivo da câmera A corresponde a qual da câmera B, calcula a diferença de tempo entre eles e gera cópias cortadas para que cada par comece exatamente no mesmo instante. Assim, os vídeos já entram alinhados na edição.

## Como funciona

1. **Extrai o áudio.** Lê os primeiros 180 segundos de cada vídeo, em mono e a 8 kHz.
2. **Mede a diferença de tempo.** Compara o áudio de cada arquivo da câmera A com cada arquivo da câmera B por correlação cruzada. O instante do pico é localizado com GCC-PHAT, que sofre menos com ruído e eco, e a força da correlação nesse ponto vira a nota de confiança do par.
3. **Descarta combinações impossíveis.** Quando os arquivos têm o metadado `creation_time`, estima a diferença fixa entre os relógios das duas câmeras e ignora pares cujo horário não bate com ela.
4. **Forma os pares.** Escolhe primeiro as combinações de maior confiança, sem repetir arquivo. O que sobra fica sem par.
5. **Confere o resultado** (só na interface gráfica). Recalcula a diferença no meio e no fim do vídeo para detectar casamentos errados ou deriva de relógio, marca diferenças fora do padrão do lote e mostra tudo para você confirmar antes de gerar os vídeos.
6. **Corta os vídeos.** Usa o `ffmpeg` para remover o início de quem começou a gravar antes e o final do mais longo, deixando os dois com a mesma duração.

## Instalação

Requer macOS. Os vídeos são codificados com o hardware do Mac por padrão (`h264_videotoolbox`).

1. Baixe ou clone este repositório.
2. Dê dois cliques em `Instalar.command`. Ele instala, quando necessário, o Homebrew, o `ffmpeg` e o Python 3.14, e cria um ambiente `.venv` com `numpy` e `scipy`.
3. Instale o suporte a janelas do Python, que o instalador ainda não inclui:

   ```bash
   brew install python-tk@3.14
   ```

## Como usar

### Interface gráfica

Abra `SyncVideosGUI.app` ou `Abrir Sincronizador.command` e siga estes passos:

1. Escolha como os vídeos estão organizados:
   - **Já separados em 2 pastas:** uma pasta para cada câmera.
   - **Pasta única:** o programa separa as câmeras por resolução e fps, pelo prefixo do nome ou por um salto na numeração do arquivo, e pede sua confirmação.
2. Escolha a pasta de saída. Se deixar em branco, ele cria `sincronizado_<data>_<hora>` ao lado da pasta dos vídeos.
3. Clique em **Sincronizar**, confira os pares detectados e confirme.

### Linha de comando

```bash
.venv/bin/python3 sync_videos.py <pasta_camera_A> <pasta_camera_B> <pasta_saida> [opções]
```

| Opção | O que faz |
|---|---|
| `--dry-run` | Só mostra os pares e as diferenças de tempo, sem gerar vídeos |
| `--pairs arquivo.csv` | Força os pares, com uma linha `video_A.mp4,video_B.mp4` para cada um |
| `--min-confidence N` | Confiança mínima para sincronizar (padrão: `0.08`) |
| `--analyze-seconds N` | Segundos analisados no início de cada vídeo (padrão: `180`) |
| `--sample-rate N` | Taxa de amostragem da análise (padrão: `8000`) |
| `--copy` | Corta sem recodificar: mais rápido, mas o corte pode cair no keyframe mais próximo |
| `--software-encoder` | Usa `libx264` em vez do codificador de hardware do Mac |
| `--no-equal-length` | Não corta o final para igualar a duração do par |

A linha de comando não faz a conferência do passo 5: ela decide só pela confiança mínima. Pares abaixo do limite são pulados.

### Arrastar e soltar (`SyncVideos.app`)

Esta versão não funciona fora da máquina original: o caminho do script está fixo em `SyncVideos.applescript` e ela usa o `/usr/bin/python3`, sem o `.venv`. Prefira a interface gráfica.

## O que é gerado

| Arquivo | Conteúdo |
|---|---|
| `take01_<nome_original>` | Vídeos sincronizados, um par para cada número de take |
| `pairing_report.csv` | Pares, diferença de tempo, confiança e status |
| `avisos.txt` | Pares a conferir e erros (só na interface gráfica, quando houver) |
| `progress.txt`, `DONE`, `FAILED` | Andamento da execução (só na linha de comando) |

Os vídeos originais não são alterados.

## Limitações

- Trabalha com exatamente duas câmeras, e os dois arquivos do par precisam ter trilha de áudio.
- Só lê os arquivos diretamente dentro da pasta escolhida, sem entrar em subpastas. Formatos aceitos: `.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.mts`, `.m2ts` e `.mxf`.
- Por padrão, os dois áudios precisam ter trechos em comum nos primeiros 180 segundos analisados. Se as gravações só se sobrepõem depois disso, aumente `--analyze-seconds`.
- A deriva de relógio entre as câmeras é só detectada, não corrigida: o programa aplica uma única diferença de tempo ao vídeo inteiro.
- Na pasta de saída, arquivos com o mesmo nome são sobrescritos sem aviso.
