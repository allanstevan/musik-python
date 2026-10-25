# musik-python

Tocador de MP3 no terminal com cara de `TREE.COM` do MS-DOS misturado com uma
tela CA-Clipper Summer 87 (menu de pulldown, sombra, rodapé de F-keys). Além
de navegar pastas de música locais, tem uma vista "Internet" que busca e
toca áudio do YouTube (via `yt-dlp`), com fila, histórico, playlists e
download opcional. Tudo numa TUI `curses` só com a biblioteca padrão do
Python pra UI — sem dependências de terceiros em Python.

```
  Arquivo   Disco   Tocar   Internet   Ajuda                         14:32
 /home/voce/Music
╔══ MUSIC (23) ═════════════════════════════════════════════════════════╗
║ ├───ATERCIOPELADOS                                                    ║
║ ├───BOB_MARLEY                                                        ║
║ └───♪ RAMMSTEIN                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
╔══ Tocando ════════════════════════════════════════════════════════════╗
║ Faixa : Du Hast                                              [Tocando]║
╚═══════════════════════════════════════════════════════════════════════╝
```

## Requisitos

- Python 3.8+ (só biblioteca padrão)
- [`mpv`](https://mpv.io) — toca o áudio (local e streaming)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) — só se for usar a aba
  **Internet** (busca/toca YouTube). Sem áudio local funciona sem ele.

```bash
# Debian/Ubuntu
sudo apt install mpv yt-dlp

# Arch
sudo pacman -S mpv yt-dlp

# macOS (Homebrew)
brew install mpv yt-dlp
```

## Como rodar

```bash
python3 music.py                # tenta achar a pasta de música sozinho
python3 music.py ~/Minhas/MP3s  # ou aponta a pasta na mão
```

Na primeira vez sem argumento, ele procura em `/mnt/sd/Music` e depois em
`~/Music`. Se não achar nada, use o argumento ou adicione uma pasta depois
pelo menu **Disco → Adicionar pasta...** (dá pra somar quantas pastas
quiser — disco local, outro HD, um compartilhamento de rede já montado —
todas aparecem juntas numa "BIBLIOTECA" combinada).

Mídia removível (pendrive, SD card) plugada depois é detectada: o menu
**Disco** sugere as pastas de música encontradas nela pra trocar ou somar.

## Atalhos principais

| Tecla | Ação |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | mover na lista |
| `→` / `Enter` numa pasta | expandir |
| `←` | colapsar / subir |
| `Enter` numa faixa | tocar |
| `N` / `P` | próxima / anterior |
| `Espaço` | pausar / continuar |
| `S` | parar |
| `+` `-` | volume |
| `Y` (`F3`) | shuffle |
| `T` (`F4`) | repeat (off → faixa → pasta) |
| `/` | buscar na árvore (`Esc` limpa) |
| `R` (`F2`) | re-scan da pasta |
| `I` (`F5`) | alternar árvore / Internet |
| `K` | trocar skin |
| `Q` (`F10`) | sair |

Também dá pra usar o mouse: clica no menu, nas faixas, na barra de volume,
na barra de progresso e no rodapé.

### Vista Internet

| Tecla | Ação |
|---|---|
| `B` | buscar e tocar do YouTube |
| `+` (na Internet) | enfileirar outra |
| `V` | salvar a fila atual como playlist |
| `D` | baixar MP3 on/off (baixa e converte o que tocar) |
| `J` | "Jogo do Inter" — toca a live atual do canal configurado (erro
  "Sem Live" se não tiver nenhuma ao vivo) |
| `Esc` | voltar pra árvore local |

Pelo menu **Internet** / botões da vista dá pra abrir **Histórico** (tudo
que já tocou da internet) e **Playlists** (salvar/abrir/"Sair da
Playlist"). O destino dos downloads é `<pasta atual>/Internet` por padrão,
mas pode ser trocado em **Disco → Pasta de downloads...**.

## Skins

Menu **Arquivo** ou tecla `K` cicla entre 4 esquemas de cor (fica salvo e
volta na mesma na próxima vez que abrir):

- **Azul** (padrão) — visual CGA/DOS azul clássico.
- **PC-XT** — mono fósforo verde, estilo monitor CGA/Hercules dos anos 80.
- **Vermelha** — a mesma paleta da Azul, só com o fundo em tons de vermelho.
- **Preta** — mono fósforo branco (preto e branco puro).

Versão e data do build aparecem em **Arquivo → Sobre...**.

## Arquivos de estado

- `~/.config/music/state.json` — última pasta/faixa, volume, shuffle,
  repeat, skin, pastas extras adicionadas.
- `~/.config/music/internet.json` — histórico, playlists e preferência de
  download da aba Internet.
- `~/.cache/music/library-*.json` — cache da árvore de pastas (um por
  pasta raiz, pra re-scan não precisar reler tudo do disco).

Pode apagar qualquer um desses arquivos pra resetar aquele pedaço do
estado sem afetar o resto.
