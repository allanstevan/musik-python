#!/usr/bin/env python3
"""MUSIC.EXE — tocador de MP3 com cara de TREE.COM + CA-Clipper Summer 87.

Motor (mpv por IPC, curses put()/box()) copiado de radio.py. Em vez de abas
de estação, isso aqui navega uma árvore de pastas (estilo `TREE /F` do
MS-DOS 5) tocando arquivos locais. Também busca e toca áudio do YouTube
via yt-dlp (vista "Internet"), com fila, histórico, playlists e download.
"""
import curses
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata

VERSION = "1.0"
BUILD_DATE = "2026-10-25"

AUDIO_EXTS = (".mp3", ".ogg", ".flac", ".m4a", ".wav")
CACHE_DIR = os.path.expanduser("~/.cache/music")
STATE = os.path.expanduser("~/.config/music/state.json")
INTERNET_STORE = os.path.expanduser("~/.config/music/internet.json")
HISTORY_MAX = 50
SOCK = os.path.join(tempfile.gettempdir(), f"music-mpv-{os.getuid()}.sock")

MUSIC_NAME_RE = re.compile(r"music|m[uú]sicas?|mp3", re.I)
BLACKLIST = {"dcim", "video", "videos", "android", "download", "downloads",
             "pem", "keys", "system", ".android_secure", "lost+found"}

YTDLP_CLIENT_ARGS = ["--extractor-args", "youtube:player_client=android"]
BALDASSO_LIVE_URL = "https://www.youtube.com/channel/UC4QpkZzN08pFibmJAl213-w/live"


# ---------------------------------------------------------------- achar pasta
def resolve_root(argv):
    tried = []
    if len(argv) > 1:
        p = os.path.abspath(os.path.expanduser(argv[1]))
        tried.append(p)
        if os.path.isfile(p):
            return os.path.dirname(p), p, tried
        if os.path.isdir(p):
            return p, None, tried
    for p in ("/mnt/sd/Music", os.path.expanduser("~/Music")):
        tried.append(p)
        if os.path.isdir(p):
            return p, None, tried
    return None, None, tried


def scan_removable():
    suggestions = []
    seen = set()
    for pattern in ("/media/*/*", "/media/*", "/run/media/*/*", "/mnt/*"):
        for m in glob.glob(pattern):
            if not os.path.isdir(m) or m in seen:
                continue
            seen.add(m)
            if os.path.basename(m).lower() in BLACKLIST:
                continue
            try:
                entries = os.listdir(m)
            except OSError:
                continue
            for e in entries:
                if e.lower() in BLACKLIST:
                    continue
                full = os.path.join(m, e)
                if os.path.isdir(full) and MUSIC_NAME_RE.search(e):
                    suggestions.append(full)
            if any(e.lower().endswith(AUDIO_EXTS) for e in entries
                   if os.path.isfile(os.path.join(m, e))):
                suggestions.append(m)
    out = []
    for s in suggestions:
        if s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- estado
def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass


def fold(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------- internet
def load_internet():
    try:
        with open(INTERNET_STORE) as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("history", [])
    d.setdefault("playlists", {})
    d.setdefault("download", False)
    return d


def save_internet(d):
    try:
        os.makedirs(os.path.dirname(INTERNET_STORE), exist_ok=True)
        with open(INTERNET_STORE, "w") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _persist_copy(item):
    return {k: item.get(k) for k in ("title", "duration", "video_id", "webpage_url", "uploader")}


def ytdlp_resolve_start(ref):
    if ref.get("video_id"):
        target = f"https://www.youtube.com/watch?v={ref['video_id']}"
    elif ref.get("channel_live"):
        target = ref["channel_live"]
    elif ref.get("webpage_url"):
        target = ref["webpage_url"]
    else:
        target = "ytsearch1:" + ref.get("query", "")
    cmd = (["yt-dlp", "-f", "bestaudio/best", "--no-playlist", "-j"]
           + YTDLP_CLIENT_ARGS + [target])
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    os.set_blocking(proc.stdout.fileno(), False)
    return proc


def ytdlp_resolve_drain(proc, buf):
    # yt-dlp -j despeja o info dict completo (todos os formatos, storyboards
    # etc.) — pode passar de várias centenas de KB. Sem drenar o pipe aqui a
    # cada iteração, o processo trava no write() quando o buffer do SO
    # enche (~64KB) e ninguém está lendo — deadlock. Lemos sempre, em modo
    # não-bloqueante, e só processamos quando o processo já terminou.
    try:
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            buf += chunk
    except (BlockingIOError, InterruptedError):
        pass
    except Exception:
        pass
    return buf


def ytdlp_resolve_parse(buf, returncode):
    if returncode != 0:
        return None
    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not d.get("url"):
                continue
            return {"title": d.get("title") or "?", "duration": d.get("duration"),
                     "video_id": d.get("id"), "webpage_url": d.get("webpage_url"),
                     "uploader": d.get("uploader"), "url": d.get("url"),
                     "is_live": bool(d.get("is_live"))}
    return None


def ytdlp_download_start(item, dest_dir):
    target = (item.get("webpage_url")
              or (f"https://www.youtube.com/watch?v={item['video_id']}" if item.get("video_id") else None)
              or "ytsearch1:" + item.get("title", ""))
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return None
    out_tmpl = os.path.join(dest_dir, "%(title)s.%(ext)s")
    cmd = (["yt-dlp", "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
            "--no-playlist", "-o", out_tmpl] + YTDLP_CLIENT_ARGS + [target])
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


# ---------------------------------------------------------------- scan da árvore
def _walk(path):
    try:
        entries = os.listdir(path)
    except OSError:
        return []
    dirs, files = [], []
    for e in entries:
        if e.startswith("."):
            continue
        full = os.path.join(path, e)
        if os.path.isdir(full):
            dirs.append(e)
        elif e.lower().endswith(AUDIO_EXTS):
            files.append(e)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    children = []
    for d in dirs:
        dp = os.path.join(path, d)
        children.append({"name": d, "path": dp, "type": "dir", "children": _walk(dp)})
    for fn in files:
        fp = os.path.join(path, fn)
        children.append({"name": os.path.splitext(fn)[0], "path": fp, "type": "file"})
    return children


def _cache_path(root):
    h = hashlib.md5(os.path.abspath(root).encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"library-{h}.json")


def scan(root, force=False):
    try:
        sig = os.path.getmtime(root)
    except OSError:
        sig = None
    cache_file = _cache_path(root)
    if not force:
        try:
            with open(cache_file) as f:
                c = json.load(f)
            if c.get("root") == root and c.get("mtime") == sig:
                return c["tree"]
        except Exception:
            pass
    tree = {"name": os.path.basename(root.rstrip("/")) or root, "path": root,
            "type": "dir", "children": _walk(root)}
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump({"root": root, "mtime": sig, "tree": tree}, f, ensure_ascii=False)
    except Exception:
        pass
    return tree


def all_dirs(n):
    paths = [n["path"]] if n["type"] == "dir" else []
    for c in n.get("children", ()):
        paths += all_dirs(c)
    return paths


def find_node(n, path):
    if n["path"] == path:
        return n, []
    for c in n.get("children", ()):
        r = find_node(c, path)
        if r:
            node, chain = r
            return node, [n["path"]] + chain
    return None


def filtered_tree(node, query):
    if not query:
        return node
    q = fold(query)
    def rec(n):
        if n["type"] == "file":
            return n if q in fold(n["name"]) else None
        if q in fold(n["name"]):
            return n
        kept = [r for r in (rec(c) for c in n["children"]) if r]
        if kept:
            return {"name": n["name"], "path": n["path"], "type": "dir", "children": kept}
        return None
    r = rec(node)
    return r or {"name": node["name"], "path": node["path"], "type": "dir", "children": []}


def flatten(node, expanded, depth=0, ancestors_last=(), parent=None):
    rows = []
    children = node.get("children", ())
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        rows.append({"node": child, "depth": depth, "is_last": is_last,
                      "ancestors_last": ancestors_last, "parent": parent})
        if child["type"] == "dir" and child["path"] in expanded:
            rows.extend(flatten(child, expanded, depth + 1,
                                 ancestors_last + (is_last,), child["path"]))
    return rows


def tree_prefix(row):
    prefix = "".join("    " if al else "│   " for al in row["ancestors_last"])
    connector = "└───" if row["is_last"] else "├───"
    return prefix + connector


# ---------------------------------------------------------------- mpv
class Mpv:
    def __init__(self, volume):
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        self.proc = subprocess.Popen(
            ["mpv", "--idle=yes", "--no-video", "--no-terminal",
             "--input-ipc-server=" + SOCK, f"--volume={volume}",
             "--audio-client-name=MUSIC.EXE"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.sock = None
        for _ in range(50):
            try:
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(SOCK)
                break
            except OSError:
                self.sock = None
                time.sleep(0.1)
        if self.sock is None:
            raise RuntimeError("não consegui falar com o mpv")
        self.sock.setblocking(False)
        self.buf = b""
        self.events = []
        self.props = {}
        for i, p in enumerate(("media-title", "pause", "volume", "paused-for-cache",
                                "idle-active", "time-pos", "duration", "metadata"), 1):
            self.cmd("observe_property", i, p)

    def cmd(self, *args):
        try:
            self.sock.sendall(json.dumps({"command": list(args)}).encode() + b"\n")
        except OSError:
            pass

    def poll(self):
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buf += chunk
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            return
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "property-change":
                self.props[msg["name"]] = msg.get("data")
            elif "event" in msg:
                self.events.append(msg)

    def play(self, path):
        self.cmd("loadfile", path, "replace")
        self.cmd("set_property", "pause", False)

    def stop(self):
        self.cmd("stop")

    def quit(self):
        self.cmd("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.unlink(SOCK)
        except OSError:
            pass


# ---------------------------------------------------------------- UI
(C_BG, C_BAR, C_SEL, C_TITLE, C_KEY, C_DIM, C_PLAY, C_ERR,
 C_FOLDER, C_SHADOW, C_MENU) = range(1, 12)

SKINS = ["Azul", "PC-XT", "Vermelha", "Preta"]


def skin_colors(skin):
    """(B,W,K,Y,C,G,R) — papéis de cor usados por init_colors().
    B=fundo, W=branco/destaque, K=preto, Y=título/pasta, C=info/dim, G=tocando, R=erro."""
    hi = curses.COLORS >= 256
    if skin == "PC-XT":
        # mono CGA fósforo verde: tudo no mesmo verde, fundo preto
        return (16, 46, 16, 46, 28, 46, 46) if hi else (
            curses.COLOR_BLACK, curses.COLOR_GREEN, curses.COLOR_BLACK,
            curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_GREEN)
    if skin == "Vermelha":
        # igual à Azul, só o fundo troca de azul pra vermelho
        return (88, 231, 16, 227, 37, 83, 203) if hi else (
            curses.COLOR_RED, curses.COLOR_WHITE, curses.COLOR_BLACK,
            curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_RED)
    if skin == "Preta":
        # mono CGA fósforo branco: tudo em branco/cinza, fundo preto
        return (16, 231, 16, 231, 250, 231, 231) if hi else (
            curses.COLOR_BLACK, curses.COLOR_WHITE, curses.COLOR_BLACK,
            curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_WHITE)
    # "Azul" — paleta CGA/DOS de verdade (o "azul" de cada terminal varia muito)
    return (19, 231, 16, 227, 37, 83, 203) if hi else (
        curses.COLOR_BLUE, curses.COLOR_WHITE, curses.COLOR_BLACK,
        curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_RED)


def init_colors(skin="Azul"):
    curses.start_color()
    curses.use_default_colors()
    B, W, K, Y, C, G, R = skin_colors(skin)
    curses.init_pair(C_BG, W, B)        # texto branco no azul Clipper
    curses.init_pair(C_BAR, K, C)       # barras: preto no ciano
    curses.init_pair(C_SEL, K, C)       # seleção: preto no ciano (reverse)
    curses.init_pair(C_TITLE, Y, B)     # títulos: amarelo no azul
    curses.init_pair(C_KEY, K, W)       # número de F-key: preto no branco
    curses.init_pair(C_DIM, C, B)       # texto discreto: ciano no azul
    curses.init_pair(C_PLAY, G, B)      # faixa tocando: verde no azul
    curses.init_pair(C_ERR, R, B)       # erro: vermelho no azul
    curses.init_pair(C_FOLDER, Y, B)    # pasta: amarelo no azul
    curses.init_pair(C_SHADOW, K, K)    # sombra Clipper: preto no preto
    curses.init_pair(C_MENU, K, W)      # barra de menu: preto no branco


def put(win, y, x, text, attr=0, width=None):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if width is not None:
        text = text[:width].ljust(width)
    text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def box(win, y, x, h, w, title, attr):
    put(win, y, x, "╔" + "═" * (w - 2) + "╗", attr)
    for i in range(1, h - 1):
        put(win, y + i, x, "║", attr)
        put(win, y + i, x + w - 1, "║", attr)
    put(win, y + h - 1, x, "╚" + "═" * (w - 2) + "╝", attr)
    if title:
        put(win, y, x + 3, f" {title} ", curses.color_pair(C_TITLE) | curses.A_BOLD)


def box_thin(win, y, x, h, w, attr):
    put(win, y, x, "┌" + "─" * (w - 2) + "┐", attr)
    for i in range(1, h - 1):
        put(win, y + i, x, "│", attr)
        put(win, y + i, x + w - 1, "│", attr)
    put(win, y + h - 1, x, "└" + "─" * (w - 2) + "┘", attr)


def vol_bar(v, n=20):
    v = max(0, min(100, int(v or 0)))
    full = round(v / 100 * n)
    return "█" * full + "░" * (n - full) + f" {v:3d}%"


def fmt_time(s):
    if s is None:
        return "--:--"
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def progress_bar(pos, dur, n=28):
    if not dur:
        return "░" * n
    frac = max(0.0, min(1.0, (pos or 0) / dur))
    full = round(frac * n)
    return "█" * full + "░" * (n - full)


def meta_get(meta, key):
    if not isinstance(meta, dict):
        return None
    for k, v in meta.items():
        if k.lower() == key:
            return v
    return None


HELP = [
    "↑ ↓ PgUp PgDn Home End   mover na lista",
    "→  / Enter numa pasta    expandir",
    "←                        colapsar / subir",
    "Enter numa faixa         tocar",
    "N / P                    próxima / anterior",
    "Espaço                   pausar / continuar",
    "S                        parar",
    "+ / -                    volume",
    "Y   (F3)                 shuffle on/off",
    "T   (F4)                 repeat off/faixa/pasta",
    "/                        buscar na árvore (Esc limpa)",
    "R   (F2)                 re-scan da pasta",
    "I   (F5)                 alternar árvore / Internet",
    "Esc (na Internet)        voltar pra árvore local",
    "B                        Internet: buscar e tocar",
    "+   (na Internet)        Internet: enfileirar",
    "V                        Internet: salvar playlist",
    "D                        Internet: baixar MP3 on/off",
    "J                        Internet: Jogo do Inter (live)",
    "Menu Disco               adicionar/remover pasta, pasta de downloads",
    "K                        trocar skin (Azul/PC-XT/Vermelha/Preta)",
    "Menu Arquivo             skins e Sobre (versão)",
    "Q   (F10)                sair",
    "Mouse: clica no menu, faixas, barras e rodapé",
]


# ---------------------------------------------------------------- main
def main(scr, root, start_file):
    curses.curs_set(0)
    state = load_state()
    skin = state.get("skin") if state.get("skin") in SKINS else "Azul"
    init_colors(skin)
    scr.bkgd(" ", curses.color_pair(C_BG))
    scr.timeout(250)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(0)

    internet_store = load_internet()
    extra_roots = [p for p in state.get("extra_roots", []) if os.path.isdir(p)]
    tree = None
    durations = {}

    expanded = set()
    sel = top = 0
    query, searching, text_mode = "", False, None
    playing_path, status = None, "Parado"
    playing_source, playing_item = "local", None
    shuffle = bool(state.get("shuffle", False))
    repeat = state.get("repeat", "off")
    shuffle_bag = []
    show_help = False
    show_about = False
    user_stopped = False

    view = "tree"
    active_menu = None
    show_history = False
    show_playlists = False
    internet_queue = []
    resolve_requests = []
    active_resolve = None
    download_procs = []

    mpv = Mpv(state.get("volume", 80))

    def active_roots():
        rs = [root] + [r for r in extra_roots if os.path.abspath(r) != os.path.abspath(root)]
        return [r for r in rs if os.path.isdir(r)]

    def rebuild_tree(force=False):
        nonlocal tree
        roots_now = active_roots()
        if len(roots_now) <= 1:
            tree = scan(roots_now[0] if roots_now else root, force=force)
            return
        seen_names, children = {}, []
        for r in roots_now:
            sub = scan(r, force=force)
            name = sub.get("name") or os.path.basename(r.rstrip("/")) or r
            if name in seen_names:
                name = f"{name} ({os.path.basename(os.path.dirname(r.rstrip('/'))) or r})"
            seen_names[name] = True
            children.append({**sub, "name": name})
        tree = {"name": "Biblioteca", "path": "::multi::", "type": "dir", "children": children}

    rebuild_tree(force=False)

    def visible_root():
        return filtered_tree(tree, query) if query else tree

    def visible_expanded():
        return set(all_dirs(visible_root())) if query else expanded

    def rows_for(vroot, vexp):
        return flatten(vroot, vexp)

    def find_row_index(rows, path):
        for i, r in enumerate(rows):
            if r["node"]["path"] == path:
                return i
        return None

    last = state.get("last")
    if last:
        found = find_node(tree, last)
        if found:
            _, chain = found
            expanded.update(chain)
            rows0 = rows_for(tree, expanded)
            idx = find_row_index(rows0, last)
            if idx is not None:
                sel = idx

    def play_row(row):
        nonlocal playing_path, status, user_stopped, playing_source, playing_item
        if row["node"]["type"] != "file":
            return
        playing_path = row["node"]["path"]
        playing_source, playing_item = "local", None
        status = "Carregando…"
        user_stopped = False
        mpv.props.pop("media-title", None)
        mpv.props.pop("metadata", None)
        if not os.path.exists(playing_path):
            status = "ERRO: arquivo sumiu"
            return
        mpv.play(playing_path)
        state["last"] = playing_path
        save_state(state)

    def play_internet_now(ref, label):
        resolve_requests.append({"mode": "play", "ref": ref, "label": label})

    def play_jogo_inter():
        resolve_requests.append({"mode": "play", "ref": {"channel_live": BALDASSO_LIVE_URL},
                                  "label": "Jogo do Inter", "fail_msg": "Sem Live"})

    def internet_advance():
        nonlocal playing_path, playing_item, playing_source, status
        if internet_queue:
            nxt = internet_queue.pop(0)
            ref = {"video_id": nxt["video_id"]} if nxt.get("video_id") else {"query": nxt["title"]}
            resolve_requests.append({"mode": "play", "ref": ref, "label": nxt["title"]})
        else:
            mpv.stop()
            mpv.props.pop("media-title", None)
            mpv.props.pop("metadata", None)
            playing_path, playing_item, playing_source, status = None, None, "local", "Parado"

    def load_playlist(name):
        nonlocal status
        items = internet_store.get("playlists", {}).get(name) or []
        if not items:
            status = f'Playlist vazia: "{name}"'
            return
        internet_queue.clear()
        first, rest = items[0], items[1:]
        ref = {"video_id": first["video_id"]} if first.get("video_id") else {"query": first["title"]}
        play_internet_now(ref, first["title"])
        internet_queue.extend(rest)
        status = f'Playlist aberta: "{name}" ({len(items)} faixas)'

    def exit_playlist():
        nonlocal playing_path, playing_item, playing_source, status, active_resolve
        internet_queue.clear()
        resolve_requests.clear()
        if active_resolve is not None:
            if active_resolve["proc"].poll() is None:
                active_resolve["proc"].kill()
            active_resolve = None
        if playing_source == "internet":
            mpv.stop()
            mpv.props.pop("media-title", None)
            mpv.props.pop("metadata", None)
            playing_path, playing_item, playing_source = None, None, "local"
        status = "Playlist encerrada"

    def internet_dl_dir():
        override = internet_store.get("download_dir")
        if override:
            try:
                os.makedirs(override, exist_ok=True)
                if os.access(override, os.W_OK):
                    return override
            except OSError:
                pass
        try:
            if os.access(root, os.W_OK):
                return os.path.join(root, "Internet")
        except OSError:
            pass
        return os.path.expanduser("~/Music/Internet")

    def maybe_download(item):
        if item.get("_dl_started") or item.get("is_live"):
            return
        dp = ytdlp_download_start(item, internet_dl_dir())
        if dp:
            item["_dl_started"] = True
            download_procs.append({"proc": dp, "label": item.get("title") or "?"})

    def toggle_download():
        nonlocal status
        on = not internet_store.get("download")
        internet_store["download"] = on
        save_internet(internet_store)
        if on and playing_source == "internet" and playing_item:
            maybe_download(playing_item)

    def current_tracks_and_index(rows):
        tracks = [r for r in rows if r["node"]["type"] == "file"]
        idx = None
        for i, t in enumerate(tracks):
            if t["node"]["path"] == playing_path:
                idx = i
                break
        return tracks, idx

    def advance(direction):
        nonlocal sel, shuffle_bag
        rows = rows_for(visible_root(), visible_expanded())
        tracks, idx = current_tracks_and_index(rows)
        if not tracks:
            return
        if repeat == "faixa" and idx is not None and direction == 1:
            play_row(tracks[idx])
            return
        if shuffle and direction == 1:
            if not shuffle_bag:
                shuffle_bag = list(range(len(tracks)))
                import random
                random.shuffle(shuffle_bag)
            nxt = shuffle_bag.pop(0)
        else:
            if idx is None:
                nxt = 0
            else:
                nxt = idx + direction
                if nxt < 0:
                    if repeat == "pasta":
                        nxt = len(tracks) - 1
                    else:
                        return
                elif nxt >= len(tracks):
                    if repeat == "pasta":
                        nxt = 0
                    else:
                        return
        play_row(tracks[nxt])
        full_idx = find_row_index(rows, tracks[nxt]["node"]["path"])
        if full_idx is not None:
            sel = full_idx

    if start_file and os.path.isfile(start_file):
        found = find_node(tree, start_file)
        if found:
            node, chain = found
            expanded.update(chain)
            rows0 = rows_for(tree, expanded)
            idx = find_row_index(rows0, start_file)
            if idx is not None:
                sel = idx
                play_row(rows0[idx])

    while True:
        mpv.poll()
        for ev in mpv.events:
            if ev.get("event") == "end-file":
                reason = ev.get("reason")
                if reason == "eof" and not user_stopped:
                    if playing_source == "internet":
                        internet_advance()
                    else:
                        advance(1)
                elif reason == "error":
                    status = "ERRO: falha ao tocar"
                    if not user_stopped:
                        if playing_source == "internet":
                            internet_advance()
                        else:
                            advance(1)
            elif ev.get("event") == "file-loaded":
                status = "Tocando"
        mpv.events.clear()
        p = mpv.props
        if playing_path and status in ("Tocando", "Pausado", "Carregando…"):
            if p.get("pause"):
                status = "Pausado"
            elif p.get("paused-for-cache"):
                status = "Carregando…"
            else:
                status = "Tocando"
        if playing_path and p.get("duration"):
            durations[playing_path] = p["duration"]

        done_dls = [d for d in download_procs if d["proc"].poll() is not None]
        download_procs[:] = [d for d in download_procs if d["proc"].poll() is None]
        for d in done_dls:
            status = (f"Baixado: {d['label']}" if d["proc"].returncode == 0
                       else f"ERRO ao baixar: {d['label']}")

        if active_resolve is None and resolve_requests:
            req = resolve_requests.pop(0)
            proc = ytdlp_resolve_start(req["ref"])
            if proc is None:
                status = "ERRO: yt-dlp não encontrado"
            else:
                active_resolve = {"proc": proc, "req": req, "buf": b"", "t0": time.time()}
                status = "Buscando…" if req["mode"] == "play" else f"Enfileirando \"{req['label']}\"…"
        elif active_resolve is not None:
            active_resolve["buf"] = ytdlp_resolve_drain(active_resolve["proc"], active_resolve["buf"])
            timed_out = time.time() - active_resolve["t0"] > 45
            if timed_out and active_resolve["proc"].poll() is None:
                active_resolve["proc"].kill()
            if timed_out or active_resolve["proc"].poll() is not None:
                active_resolve["buf"] = ytdlp_resolve_drain(active_resolve["proc"], active_resolve["buf"])
                req = active_resolve["req"]
                item = None if timed_out else ytdlp_resolve_parse(
                    active_resolve["buf"], active_resolve["proc"].poll() or 0)
                active_resolve = None
                if not item:
                    status = req.get("fail_msg") or f"ERRO: nao achei \"{req['label']}\""
            else:
                item = None
            if item:
                hist_entry = _persist_copy(item)
                hist_entry["played_at"] = time.strftime("%Y-%m-%d %H:%M")
                internet_store["history"].insert(0, hist_entry)
                internet_store["history"] = internet_store["history"][:HISTORY_MAX]
                save_internet(internet_store)
                if internet_store.get("download"):
                    maybe_download(item)
                if req["mode"] == "play":
                    playing_source, playing_item = "internet", item
                    playing_path = item["url"]
                    durations[playing_path] = item.get("duration")
                    user_stopped = False
                    status = "Carregando…"
                    mpv.play(item["url"])
                else:
                    internet_queue.append(item)
                    status = f"Enfileirado: {item['title']}"

        rows, display_list = [], []
        if view == "tree":
            vroot = visible_root()
            vexp = visible_expanded()
            rows = rows_for(vroot, vexp)
            sel = max(0, min(sel, len(rows) - 1)) if rows else 0
        else:
            if playing_source == "internet" and playing_item:
                display_list.append(("playing", playing_item))
            display_list += [("queued", it) for it in internet_queue]
            sel = max(0, min(sel, len(display_list) - 1)) if display_list else 0

        # ---------------- desenho
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 12 or w < 50:
            put(scr, 0, 0, "Tela pequena demais (mín. 50x12)", curses.color_pair(C_ERR))
            scr.refresh()
            if scr.getch() in (ord("q"), ord("Q")):
                break
            continue

        bar = curses.color_pair(C_BAR)
        menu = curses.color_pair(C_MENU)
        shadow = curses.color_pair(C_SHADOW)
        put(scr, 0, 0, " " * w, menu)
        menu_labels = [("arquivo", "Arquivo"), ("disco", "Disco"), ("tocar", "Tocar"),
                       ("internet", "Internet"), ("ajuda", "Ajuda")]
        menu_hits = {}
        mx = 1
        for key, label in menu_labels:
            item = f" {label} "
            active = active_menu == key or (key == "internet" and view == "internet")
            attr = (curses.color_pair(C_SEL) | curses.A_BOLD) if active else menu
            put(scr, 0, mx, item, attr)
            menu_hits[key] = (mx, mx + len(item))
            mx += len(item) + 1
        put(scr, 0, w - 6, time.strftime("%H:%M"), menu | curses.A_BOLD)

        if searching:
            label = {"inet_search": "Buscar", "inet_enqueue": "Enfileirar",
                     "inet_save": "Nome da playlist", "add_root": "Adicionar pasta (caminho)",
                     "set_dl_dir": "Pasta de downloads (vazio = padrao)"}.get(text_mode, "Busca")
            put(scr, 1, 1, label + ": ", curses.color_pair(C_TITLE) | curses.A_BOLD)
            put(scr, 1, 3 + len(label), query + "▌", curses.color_pair(C_BG) | curses.A_BOLD)
        elif view == "internet":
            dl = "ON" if internet_store.get("download") else "OFF"
            info = f"Fila: {len(internet_queue)}   Baixar MP3: {dl}"
            if download_procs:
                info += f"   baixando:{len(download_procs)}"
            put(scr, 1, 1, info, curses.color_pair(C_DIM) | curses.A_BOLD)
        elif query:
            put(scr, 1, 1, "Busca: ", curses.color_pair(C_TITLE) | curses.A_BOLD)
            put(scr, 1, 8, query, curses.color_pair(C_BG) | curses.A_BOLD)
        else:
            cur = rows[sel]["node"] if rows else None
            dirpath = cur["path"] if (cur and cur["type"] == "dir") else \
                os.path.dirname(cur["path"]) if cur else root
            put(scr, 1, 1, dirpath, curses.color_pair(C_DIM) | curses.A_BOLD)
            flags = []
            if shuffle:
                flags.append("SHUFFLE")
            if repeat != "off":
                flags.append(f"REPEAT:{repeat.upper()}")
            if flags:
                tag = " ".join(flags)
                put(scr, 1, max(1, w - len(tag) - 1), tag, curses.color_pair(C_TITLE) | curses.A_BOLD)

        panel_h = 6
        lh = h - 2 - panel_h - 1
        tree_rows = lh - 2
        inner_w = w - 4
        scroll_hit = None
        tree_hit = None
        queue_hit = None
        button_hits = []

        if view == "tree":
            box(scr, 2, 0, lh, w, f"{tree['name'].upper()} ({len(rows)})",
                curses.color_pair(C_BG))
            if sel < top:
                top = sel
            if sel >= top + tree_rows:
                top = sel - tree_rows + 1
            top = max(0, min(top, max(0, len(rows) - tree_rows)))
            for r in range(tree_rows):
                i = top + r
                if i >= len(rows):
                    break
                row = rows[i]
                node = row["node"]
                is_playing = playing_source == "local" and node["path"] == playing_path
                if node["type"] == "dir":
                    text = tree_prefix(row) + node["name"].upper()
                    base_attr = curses.color_pair(C_FOLDER) | curses.A_BOLD
                else:
                    dur = durations.get(node["path"])
                    mark = "♪ " if is_playing else ""
                    left = tree_prefix(row) + " " + mark + node["name"]
                    if dur:
                        tstr = "  " + fmt_time(dur)
                        left = left[: max(0, inner_w - len(tstr))].ljust(inner_w - len(tstr)) + tstr
                    text = left
                    base_attr = (curses.color_pair(C_PLAY) | curses.A_BOLD) if is_playing \
                        else curses.color_pair(C_BG)
                attr = (curses.color_pair(C_SEL) | curses.A_BOLD) if i == sel else base_attr
                put(scr, 3 + r, 2, text, attr, width=inner_w)
            if len(rows) > tree_rows and tree_rows > 0:
                pos = int((top / max(1, len(rows) - tree_rows)) * (tree_rows - 1))
                for r in range(tree_rows):
                    put(scr, 3 + r, w - 1, "█" if r == pos else "░", curses.color_pair(C_DIM))
                scroll_hit = (w - 1, 3, 3 + tree_rows - 1)
            if not rows:
                put(scr, 4, 3, "Nada encontrado.", curses.color_pair(C_DIM))
            tree_hit = (3, 3 + tree_rows - 1, 2, 2 + inner_w - 1)
        else:
            box(scr, 2, 0, lh, w, f"INTERNET — FILA ({len(internet_queue)})",
                curses.color_pair(C_BG))
            buttons = [("Buscar", "inet_search"), ("+ Enfileirar", "inet_enqueue"),
                       ("Save", "inet_save"), ("Histórico", "inet_history"),
                       ("Playlists", "inet_open_playlist"),
                       ("Jogo do Inter", "inet_jogo_inter"),
                       (f"Baixar:{'ON' if internet_store.get('download') else 'OFF'}",
                        "inet_toggle_dl")]
            bx = 2
            for label, action in buttons:
                seg = f"[{label}]"
                if bx + len(seg) + 1 >= w - 1:
                    break
                put(scr, 3, bx, seg, curses.color_pair(C_MENU) | curses.A_BOLD)
                button_hits.append((bx, bx + len(seg), action))
                bx += len(seg) + 1

            list_rows = max(1, tree_rows - 2)
            if sel < top:
                top = sel
            if sel >= top + list_rows:
                top = sel - list_rows + 1
            top = max(0, min(top, max(0, len(display_list) - list_rows)))
            for r in range(list_rows):
                i = top + r
                if i >= len(display_list):
                    break
                kind, it = display_list[i]
                dur = it.get("duration")
                mark = "♪ " if kind == "playing" else f"{i}. "
                left = " " + mark + (it.get("title") or "?")
                if dur:
                    tstr = "  " + fmt_time(dur)
                    left = left[: max(0, inner_w - len(tstr))].ljust(inner_w - len(tstr)) + tstr
                base_attr = (curses.color_pair(C_PLAY) | curses.A_BOLD) if kind == "playing" \
                    else curses.color_pair(C_BG)
                attr = (curses.color_pair(C_SEL) | curses.A_BOLD) if i == sel else base_attr
                put(scr, 5 + r, 2, left, attr, width=inner_w)
            if not display_list:
                put(scr, 5, 3, "Fila vazia. Clique [Buscar] ou tecle B.", curses.color_pair(C_DIM))
            queue_hit = (5, 5 + list_rows - 1, 2, 2 + inner_w - 1)

        # painel "Tocando"
        py = 2 + lh
        box(scr, py, 0, panel_h, w, "Tocando", curses.color_pair(C_BG))
        playing_node = None
        if playing_source == "local" and playing_path:
            f = find_node(tree, playing_path)
            playing_node = f[0] if f else None
        if playing_source == "internet" and playing_item:
            title = playing_item.get("title") or "—"
        elif not playing_path:
            title = "—"
        else:
            title = p.get("media-title")
            if not title or title == os.path.basename(playing_path):
                title = playing_node["name"] if playing_node else None
            title = title or "—"
        st_attr = (curses.color_pair(C_ERR) if status.startswith("ERRO") else
                   curses.color_pair(C_PLAY) if status == "Tocando" else
                   curses.color_pair(C_TITLE)) | curses.A_BOLD
        put(scr, py + 1, 2, "Faixa : ", curses.color_pair(C_DIM))
        put(scr, py + 1, 10, title, curses.color_pair(C_BG) | curses.A_BOLD, width=w - 32)
        put(scr, py + 1, w - 20, f"[{status}]"[:19].rjust(19), st_attr)
        if playing_source == "internet" and playing_item:
            line2 = "Origem: YouTube"
            if playing_item.get("uploader"):
                line2 += f"    Canal: {playing_item['uploader']}"
        elif playing_path:
            pasta = os.path.basename(os.path.dirname(playing_path))
            if os.path.dirname(playing_path) == root.rstrip("/"):
                pasta = os.path.basename(root.rstrip("/"))
            artist = meta_get(p.get("metadata"), "artist")
            line2 = f"Pasta : {pasta}"
            if artist:
                line2 += f"    Artista: {artist}"
        else:
            line2 = "Pasta : —"
        put(scr, py + 2, 2, line2, curses.color_pair(C_DIM), width=w - 4)
        dur = durations.get(playing_path) if playing_path else None
        pos = p.get("time-pos") if playing_path else None
        pos_str = fmt_time(pos)
        prog_bar_x = 10 + len(pos_str) + 1
        put(scr, py + 3, 2, "Progr.: ", curses.color_pair(C_DIM))
        put(scr, py + 3, 10, f"{pos_str} {progress_bar(pos, dur)} {fmt_time(dur)}",
            curses.color_pair(C_BG))
        prog_hit = (py + 3, prog_bar_x, prog_bar_x + 28, dur) if dur else None
        vol_bar_x = 10
        put(scr, py + 4, 2, "Volume: ", curses.color_pair(C_DIM))
        put(scr, py + 4, 10, vol_bar(p.get("volume", state.get("volume", 80))),
            curses.color_pair(C_BG))
        vol_hit = (py + 4, vol_bar_x, vol_bar_x + 20)

        # rodapé estilo Clipper: número preto-no-branco colado no rótulo
        put(scr, h - 1, 0, " " * w, bar)
        fkeys = [("1", "Ajuda"), ("2", "Scan"), ("3", "Shuffle"), ("4", "Repeat"),
                 ("5", "Internet"), ("6", ""), ("7", ""), ("8", ""), ("9", ""), ("10", "Sair")]
        fkey_hits = []
        x = 0
        for num, label in fkeys:
            seg = num + label
            if x + len(seg) + 1 > w:
                break
            put(scr, h - 1, x, num, curses.color_pair(C_KEY) | curses.A_BOLD)
            put(scr, h - 1, x + len(num), label + " ", bar)
            fkey_hits.append((x, x + len(seg), num))
            x += len(seg) + 1

        dd_hits = []
        if active_menu:
            def _menu_items(key):
                if key == "arquivo":
                    items = [("F2  Re-scan", "scan")]
                    for s in SKINS:
                        mark = "> " if s == skin else "  "
                        items.append((f"{mark}Skin: {s}", ("set_skin", s)))
                    items.append(("   Sobre...", "about"))
                    items.append(("F10 Sair", "quit"))
                    return items
                if key == "disco":
                    items = [(f"Trocar: {s}", ("switch_root", s))
                             for s in scan_removable() if s not in active_roots()]
                    items += [(f"Remover pasta: {r}", ("remove_root", r)) for r in extra_roots]
                    items.append(("+  Adicionar pasta...", "add_root"))
                    items.append(("   Pasta de downloads...", "set_dl_dir"))
                    return items
                if key == "tocar":
                    return [("Espaco  Pausar/Continuar", "pause"),
                            ("N       Proxima", "next"),
                            ("P       Anterior", "prev"),
                            ("S       Parar", "stop"),
                            ("Y  F3   Shuffle", "shuffle"),
                            ("T  F4   Repeat", "repeat")]
                if key == "internet":
                    dl = "ON" if internet_store.get("download") else "OFF"
                    back = "Voltar para pastas" if view == "internet" else "Abrir vista Internet"
                    return [("B  Buscar...", "inet_search"),
                            ("+  Enfileirar...", "inet_enqueue"),
                            ("V  Salvar playlist", "inet_save"),
                            ("   Abrir playlist...", "inet_open_playlist"),
                            ("   Historico", "inet_history"),
                            ("J  Jogo do Inter", "inet_jogo_inter"),
                            (f"D  Baixar MP3: {dl}", "inet_toggle_dl"),
                            (back, "inet_toggle_view")]
                if key == "ajuda":
                    return [("F1  Ajuda", "help")]
                return []
            items = _menu_items(active_menu)
            dw = min(max(len(lbl) for lbl, _ in items) + 4, w - 2)
            dh = len(items) + 2
            dx = menu_hits[active_menu][0]
            dy = 1
            if dx + dw > w:
                dx = max(0, w - dw)
            for r in range(dh):
                put(scr, dy + 1 + r, dx + 1, " " * dw, shadow)
            for r in range(dh):
                put(scr, dy + r, dx, " " * dw, bar)
            box_thin(scr, dy, dx, dh, dw, bar)
            for i, (lbl, action) in enumerate(items):
                put(scr, dy + 1 + i, dx + 2, lbl, bar, width=dw - 3)
                if action is not None:
                    dd_hits.append((dy + 1 + i, dx + 1, dx + dw - 1, action))

        hist_hits, pl_hits = [], []
        if show_history:
            hist = internet_store.get("history", [])
            hw2 = min(w - 4, 70)
            hh2 = min(h - 4, max(6, len(hist) + 4))
            hy2, hx2 = max(0, (h - hh2) // 2), max(0, (w - hw2) // 2)
            for r in range(hh2):
                put(scr, hy2 + 1 + r, hx2 + 1, " " * hw2, shadow)
            for r in range(hh2):
                put(scr, hy2 + r, hx2, " " * hw2, bar)
            box(scr, hy2, hx2, hh2, hw2, "Historico", bar)
            box_thin(scr, hy2 + 1, hx2 + 1, hh2 - 3, hw2 - 2, bar)
            visible_n = hh2 - 4
            for r in range(visible_n):
                if r >= len(hist):
                    break
                it = hist[r]
                line = f"{(it.get('played_at') or '')[:16]}  {(it.get('title') or '?')[: hw2 - 25]}"
                put(scr, hy2 + 2 + r, hx2 + 3, line, bar, width=hw2 - 4)
                hist_hits.append((hy2 + 2 + r, hx2 + 2, hx2 + hw2 - 2, r))
            if not hist:
                put(scr, hy2 + 2, hx2 + 3, "(vazio)", bar)
            put(scr, hy2 + hh2 - 2, hx2 + hw2 - 28, "clique p/ tocar . Esc sai", bar | curses.A_BOLD)

        if show_playlists:
            names = list(internet_store.get("playlists", {}).keys())
            pw2 = min(w - 4, 56)
            rows_n = 1 + max(len(names), 1)
            ph2 = min(h - 4, max(7, rows_n + 5))
            py3, px3 = max(0, (h - ph2) // 2), max(0, (w - pw2) // 2)
            for r in range(ph2):
                put(scr, py3 + 1 + r, px3 + 1, " " * pw2, shadow)
            for r in range(ph2):
                put(scr, py3 + r, px3, " " * pw2, bar)
            box(scr, py3, px3, ph2, pw2, "Playlists", bar)
            box_thin(scr, py3 + 1, px3 + 1, ph2 - 3, pw2 - 2, bar)
            put(scr, py3 + 2, px3 + 3, "« Sair da Playlist", bar | curses.A_BOLD, width=pw2 - 4)
            pl_hits.append((py3 + 2, px3 + 2, px3 + pw2 - 2, None))
            visible_n = max(1, ph2 - 6)
            for r, name in enumerate(names):
                if r >= visible_n:
                    break
                n_items = len(internet_store["playlists"][name])
                put(scr, py3 + 3 + r, px3 + 3, f"{name}  ({n_items} faixas)", bar, width=pw2 - 4)
                pl_hits.append((py3 + 3 + r, px3 + 2, px3 + pw2 - 2, name))
            if not names:
                put(scr, py3 + 3, px3 + 3, "(nenhuma playlist salva)", bar)
            put(scr, py3 + ph2 - 2, px3 + pw2 - 26, "clique p/ abrir . Esc sai", bar | curses.A_BOLD)

        if show_help:
            hw, hh = 60, len(HELP) + 6
            hy, hx = max(0, (h - hh) // 2), max(0, (w - hw) // 2)
            for r in range(hh):
                put(scr, hy + 1 + r, hx + 1, " " * hw, shadow)
            for r in range(hh):
                put(scr, hy + r, hx, " " * hw, bar)
            box(scr, hy, hx, hh, hw, "Ajuda", bar)
            box_thin(scr, hy + 1, hx + 1, hh - 3, hw - 2, bar)
            for r, line in enumerate(HELP):
                put(scr, hy + 2 + r, hx + 3, line, bar)
            put(scr, hy + hh - 2, hx + hw - 24, " pressione uma tecla ", bar | curses.A_BOLD)

        if show_about:
            about_lines = [
                "MUSIC.EXE",
                f"Versão {VERSION} — {BUILD_DATE}",
                "",
                "Tocador de MP3 (árvore local) + áudio do YouTube.",
                "Motor: mpv (IPC) + curses — visual TREE.COM/Clipper.",
            ]
            hw3, hh3 = 58, len(about_lines) + 5
            hy3, hx3 = max(0, (h - hh3) // 2), max(0, (w - hw3) // 2)
            for r in range(hh3):
                put(scr, hy3 + 1 + r, hx3 + 1, " " * hw3, shadow)
            for r in range(hh3):
                put(scr, hy3 + r, hx3, " " * hw3, bar)
            box(scr, hy3, hx3, hh3, hw3, "Sobre", bar)
            box_thin(scr, hy3 + 1, hx3 + 1, hh3 - 3, hw3 - 2, bar)
            for r, line in enumerate(about_lines):
                put(scr, hy3 + 2 + r, hx3 + 3, line, bar, width=hw3 - 4)
            put(scr, hy3 + hh3 - 2, hx3 + hw3 - 24, " pressione uma tecla ", bar | curses.A_BOLD)

        scr.refresh()

        # ---------------- teclado / mouse
        try:
            k = scr.get_wch()
        except curses.error:
            continue

        if k == curses.KEY_MOUSE:
            try:
                _, mcx, mcy, _, bstate = curses.getmouse()
            except curses.error:
                continue
            clicked = bool(bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED))
            if show_help:
                if clicked:
                    show_help = False
                continue
            if show_about:
                if clicked:
                    show_about = False
                continue
            if show_history:
                if clicked:
                    show_history = False
                    hist = internet_store.get("history", [])
                    for ry, rx0, rx1, idx in hist_hits:
                        if mcy == ry and rx0 <= mcx <= rx1:
                            if idx < len(hist):
                                it = hist[idx]
                                ref = {"video_id": it["video_id"]} if it.get("video_id") else {"query": it["title"]}
                                play_internet_now(ref, it["title"])
                            break
                continue
            if show_playlists:
                if clicked:
                    show_playlists = False
                    for ry, rx0, rx1, name in pl_hits:
                        if mcy == ry and rx0 <= mcx <= rx1:
                            if name is None:
                                exit_playlist()
                            else:
                                load_playlist(name)
                            break
                continue
            if searching:
                continue

            if bstate & curses.BUTTON4_PRESSED:
                sel -= 1
                continue
            if bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                sel += 1
                continue

            if clicked and mcy == 0:
                hit_key = None
                for key, (x0, x1) in menu_hits.items():
                    if x0 <= mcx < x1:
                        hit_key = key
                        break
                active_menu = (None if active_menu == hit_key else hit_key) if hit_key else None
                continue

            if clicked and active_menu is not None:
                want_quit = False
                for ry, rx0, rx1, action in dd_hits:
                    if mcy == ry and rx0 <= mcx <= rx1:
                        if action == "scan":
                            rebuild_tree(force=True); expanded.clear(); sel = top = 0
                        elif action == "quit":
                            want_quit = True
                        elif action == "pause":
                            if playing_path:
                                mpv.cmd("cycle", "pause")
                        elif action == "next":
                            if playing_source == "internet":
                                internet_advance()
                            else:
                                advance(1)
                        elif action == "prev":
                            advance(-1)
                        elif action == "stop":
                            mpv.stop()
                            playing_path, playing_item, playing_source = None, None, "local"
                            status, user_stopped = "Parado", True
                        elif action == "shuffle":
                            shuffle = not shuffle; shuffle_bag = []
                            state["shuffle"] = shuffle; save_state(state)
                        elif action == "repeat":
                            repeat = {"off": "faixa", "faixa": "pasta", "pasta": "off"}[repeat]
                            state["repeat"] = repeat; save_state(state)
                        elif action == "help":
                            show_help = True
                        elif action == "about":
                            show_about = True
                        elif isinstance(action, tuple) and action[0] == "set_skin":
                            skin = action[1]
                            init_colors(skin)
                            state["skin"] = skin; save_state(state)
                            status = f"Skin: {skin}"
                        elif action == "inet_search":
                            view = "internet"; text_mode, searching, query = "inet_search", True, ""
                        elif action == "inet_enqueue":
                            view = "internet"; text_mode, searching, query = "inet_enqueue", True, ""
                        elif action == "inet_save":
                            view = "internet"; text_mode, searching, query = "inet_save", True, ""
                        elif action == "inet_open_playlist":
                            view = "internet"; show_playlists = True
                        elif action == "inet_history":
                            view = "internet"; show_history = True
                        elif action == "inet_jogo_inter":
                            view = "internet"; play_jogo_inter()
                        elif action == "inet_toggle_dl":
                            toggle_download()
                        elif action == "inet_toggle_view":
                            view = "internet" if view == "tree" else "tree"
                        elif isinstance(action, tuple) and action[0] == "switch_root":
                            root = action[1]
                            rebuild_tree(force=True)
                            expanded.clear(); sel = top = 0
                            state["folder"] = root; save_state(state)
                        elif action == "add_root":
                            text_mode, searching, query = "add_root", True, ""
                        elif action == "set_dl_dir":
                            text_mode, searching, query = "set_dl_dir", True, ""
                        elif isinstance(action, tuple) and action[0] == "remove_root":
                            p = action[1]
                            if p in extra_roots:
                                extra_roots.remove(p)
                                state["extra_roots"] = extra_roots; save_state(state)
                                rebuild_tree(force=True); expanded.clear(); sel = top = 0
                        break
                active_menu = None
                if want_quit:
                    break
                continue

            if clicked and mcy == h - 1 and fkey_hits:
                action = None
                for fx0, fx1, num in fkey_hits:
                    if fx0 <= mcx < fx1:
                        action = num
                        break
                if action == "1":
                    show_help = True
                elif action == "2":
                    rebuild_tree(force=True); expanded.clear(); sel = top = 0
                elif action == "3":
                    shuffle = not shuffle; shuffle_bag = []
                    state["shuffle"] = shuffle; save_state(state)
                elif action == "4":
                    repeat = {"off": "faixa", "faixa": "pasta", "pasta": "off"}[repeat]
                    state["repeat"] = repeat; save_state(state)
                elif action == "5":
                    view = "internet" if view == "tree" else "tree"
                elif action == "10":
                    break
            elif view == "tree" and clicked and scroll_hit and mcx == scroll_hit[0] \
                    and scroll_hit[1] <= mcy <= scroll_hit[2]:
                span = max(1, scroll_hit[2] - scroll_hit[1])
                frac = (mcy - scroll_hit[1]) / span
                top = int(frac * max(0, len(rows) - tree_rows))
                sel = top
            elif view == "tree" and clicked and tree_hit and tree_hit[0] <= mcy <= tree_hit[1] \
                    and tree_hit[2] <= mcx <= tree_hit[3]:
                i = top + (mcy - tree_hit[0])
                if i < len(rows):
                    sel = i
                    node = rows[i]["node"]
                    if node["type"] == "dir":
                        if node["path"] in expanded:
                            expanded.discard(node["path"])
                        else:
                            expanded.add(node["path"])
                    else:
                        play_row(rows[i])
            elif view == "internet" and clicked and button_hits and mcy == 3:
                for bx0, bx1, action in button_hits:
                    if bx0 <= mcx < bx1:
                        if action == "inet_search":
                            text_mode, searching, query = "inet_search", True, ""
                        elif action == "inet_enqueue":
                            text_mode, searching, query = "inet_enqueue", True, ""
                        elif action == "inet_save":
                            text_mode, searching, query = "inet_save", True, ""
                        elif action == "inet_history":
                            show_history = True
                        elif action == "inet_open_playlist":
                            show_playlists = True
                        elif action == "inet_jogo_inter":
                            play_jogo_inter()
                        elif action == "inet_toggle_dl":
                            toggle_download()
                        break
            elif view == "internet" and clicked and queue_hit and queue_hit[0] <= mcy <= queue_hit[1] \
                    and queue_hit[2] <= mcx <= queue_hit[3]:
                i = top + (mcy - queue_hit[0])
                if i < len(display_list):
                    sel = i
                    kind, it = display_list[i]
                    if kind == "playing":
                        mpv.cmd("cycle", "pause")
                    else:
                        qidx = i - (1 if (playing_source == "internet" and playing_item) else 0)
                        if 0 <= qidx < len(internet_queue):
                            picked = internet_queue.pop(qidx)
                            ref = {"video_id": picked["video_id"]} if picked.get("video_id") else {"query": picked["title"]}
                            play_internet_now(ref, picked["title"])
            elif clicked and prog_hit and mcy == prog_hit[0] and prog_hit[1] <= mcx <= prog_hit[2]:
                span = max(1, prog_hit[2] - prog_hit[1])
                frac = max(0.0, min(1.0, (mcx - prog_hit[1]) / span))
                mpv.cmd("seek", frac * prog_hit[3], "absolute")
            elif clicked and vol_hit and vol_hit[1] <= mcx <= vol_hit[2] and mcy == vol_hit[0]:
                span = max(1, vol_hit[2] - vol_hit[1])
                frac = max(0.0, min(1.0, (mcx - vol_hit[1]) / span))
                mpv.cmd("set_property", "volume", round(frac * 100))
            continue

        if show_help:
            show_help = False
            continue
        if show_about:
            show_about = False
            continue
        if show_history:
            show_history = False
            continue
        if show_playlists:
            show_playlists = False
            continue

        if searching:
            if k in ("\n", "\r", curses.KEY_ENTER):
                if text_mode == "inet_search":
                    q = query.strip()
                    if q:
                        play_internet_now({"query": q}, q)
                    query, searching, text_mode = "", False, None
                elif text_mode == "inet_enqueue":
                    q = query.strip()
                    if q:
                        resolve_requests.append({"mode": "enqueue", "ref": {"query": q}, "label": q})
                    query, searching, text_mode = "", False, None
                elif text_mode == "inet_save":
                    name = query.strip() or time.strftime("playlist-%H%M")
                    items = ([playing_item] if playing_source == "internet" and playing_item else []) \
                        + list(internet_queue)
                    if items:
                        internet_store["playlists"][name] = [_persist_copy(it) for it in items]
                        save_internet(internet_store)
                        status = f"Playlist salva: {name} ({len(items)} faixas)"
                    else:
                        status = "Nada pra salvar (nada tocando/na fila)"
                    query, searching, text_mode = "", False, None
                elif text_mode == "add_root":
                    p = os.path.abspath(os.path.expanduser(query.strip())) if query.strip() else None
                    if p and os.path.isdir(p):
                        if p in active_roots():
                            status = "Pasta ja esta na biblioteca"
                        else:
                            extra_roots.append(p)
                            state["extra_roots"] = extra_roots; save_state(state)
                            rebuild_tree(force=True); expanded.clear(); sel = top = 0
                            status = f"Pasta adicionada: {p}"
                    else:
                        status = f'ERRO: pasta nao encontrada "{query.strip()}"'
                    query, searching, text_mode = "", False, None
                elif text_mode == "set_dl_dir":
                    q = query.strip()
                    if not q:
                        internet_store["download_dir"] = None
                        save_internet(internet_store)
                        status = "Downloads: pasta padrao"
                    else:
                        p = os.path.abspath(os.path.expanduser(q))
                        try:
                            os.makedirs(p, exist_ok=True)
                            internet_store["download_dir"] = p
                            save_internet(internet_store)
                            status = f"Downloads em: {p}"
                        except OSError:
                            status = f'ERRO: nao consegui usar "{p}"'
                    query, searching, text_mode = "", False, None
                else:
                    searching = False
                continue
            elif k == "\x1b":
                searching, query, text_mode = False, "", None
                continue
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query = query[:-1]
                continue
            elif isinstance(k, str) and k.isprintable():
                query += k
                if text_mode is None:
                    sel = top = 0
                continue
            elif k in (curses.KEY_UP, curses.KEY_DOWN):
                searching = False
            else:
                continue
            if searching:
                continue

        if k in ("q", "Q", curses.KEY_F10):
            break
        elif k == curses.KEY_F1 or k in ("h", "H", "?"):
            show_help = True
        elif k == curses.KEY_UP:
            sel -= 1
        elif k == curses.KEY_DOWN:
            sel += 1
        elif k == curses.KEY_PPAGE:
            sel -= tree_rows
        elif k == curses.KEY_NPAGE:
            sel += tree_rows
        elif k == curses.KEY_HOME:
            sel = 0
        elif k == curses.KEY_END:
            sel = (len(rows) - 1) if view == "tree" else (len(display_list) - 1)
        elif k == "/" and view == "tree":
            searching, text_mode = True, None
        elif k == "\x1b" and view == "internet":
            view = "tree"
        elif k == "\x1b" and query and view == "tree":
            query = ""
        elif k in ("b", "B"):
            view = "internet"
            text_mode, searching, query = "inet_search", True, ""
        elif k in ("+", "=") and view == "internet" and not searching:
            text_mode, searching, query = "inet_enqueue", True, ""
        elif k in ("v", "V"):
            text_mode, searching, query = "inet_save", True, ""
        elif k in ("d", "D"):
            toggle_download()
        elif k in ("j", "J"):
            view = "internet"
            play_jogo_inter()
        elif k in ("k", "K"):
            skin = SKINS[(SKINS.index(skin) + 1) % len(SKINS)]
            init_colors(skin)
            state["skin"] = skin; save_state(state)
            status = f"Skin: {skin}"
        elif k in ("i", "I", curses.KEY_F5):
            view = "internet" if view == "tree" else "tree"
        elif k in (curses.KEY_RIGHT,) and view == "tree" and rows and rows[sel]["node"]["type"] == "dir":
            expanded.add(rows[sel]["node"]["path"])
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if view == "tree" and rows:
                node = rows[sel]["node"]
                if node["type"] == "dir":
                    expanded.add(node["path"])
                else:
                    play_row(rows[sel])
            elif view == "internet" and display_list:
                kind, it = display_list[sel]
                if kind == "playing":
                    mpv.cmd("cycle", "pause")
                else:
                    qidx = sel - (1 if (playing_source == "internet" and playing_item) else 0)
                    if 0 <= qidx < len(internet_queue):
                        picked = internet_queue.pop(qidx)
                        ref = {"video_id": picked["video_id"]} if picked.get("video_id") else {"query": picked["title"]}
                        play_internet_now(ref, picked["title"])
        elif k == curses.KEY_LEFT and view == "tree" and rows:
            node, parent = rows[sel]["node"], rows[sel]["parent"]
            if node["type"] == "dir" and node["path"] in expanded:
                expanded.discard(node["path"])
            elif parent:
                pidx = find_row_index(rows, parent)
                if pidx is not None:
                    sel = pidx
        elif k in ("n", "N"):
            if playing_source == "internet":
                internet_advance()
            else:
                advance(1)
        elif k in ("p", "P"):
            if playing_source == "internet":
                hist = internet_store.get("history", [])
                if len(hist) > 1:
                    prev = hist[1]
                    ref = {"video_id": prev["video_id"]} if prev.get("video_id") else {"query": prev["title"]}
                    play_internet_now(ref, prev["title"])
            else:
                advance(-1)
        elif k == " " and playing_path:
            mpv.cmd("cycle", "pause")
        elif k in ("s", "S"):
            mpv.stop()
            playing_path, playing_item, playing_source = None, None, "local"
            status, user_stopped = "Parado", True
        elif k in ("+", "="):
            mpv.cmd("add", "volume", 5)
        elif k in ("-", "_"):
            mpv.cmd("add", "volume", -5)
        elif k in ("y", "Y", curses.KEY_F3):
            shuffle = not shuffle
            shuffle_bag = []
            state["shuffle"] = shuffle
            save_state(state)
        elif k in ("t", "T", curses.KEY_F4):
            repeat = {"off": "faixa", "faixa": "pasta", "pasta": "off"}[repeat]
            state["repeat"] = repeat
            save_state(state)
        elif k in ("r", "R", curses.KEY_F2):
            rebuild_tree(force=True)
            expanded.clear()
            sel = top = 0

    if mpv.props.get("volume") is not None:
        state["volume"] = int(mpv.props["volume"])
    state["folder"] = root
    save_state(state)
    mpv.quit()


if __name__ == "__main__":
    os.environ.setdefault("ESCDELAY", "25")
    root, start_file, tried = resolve_root(sys.argv)
    if root is None:
        lines = ["music.py: nenhuma pasta de música encontrada.",
                  "Tentei: " + ", ".join(tried)]
        suggestions = scan_removable()
        if suggestions:
            lines.append("Mídia removível com música detectada:")
            for s in suggestions:
                lines.append(f"  {s}")
            lines.append(f"Tente: music.py {suggestions[0]}")
        else:
            lines.append("Nenhuma mídia removível com música encontrada em /media, /run/media ou /mnt.")
            lines.append("Conecte um pendrive/SD com músicas e tente: music.py /caminho/da/pasta")
        print("\n".join(lines), file=sys.stderr)
        sys.exit(1)
    try:
        curses.wrapper(main, root, start_file)
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        print(f"music: {e}", file=sys.stderr)
        sys.exit(1)
