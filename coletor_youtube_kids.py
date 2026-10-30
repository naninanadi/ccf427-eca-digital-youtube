#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de dados do YouTube (Data API v3) focado em conteudo infantil.

IMPORTANTE: nao existe API publica do YouTube Kids. Este script usa a
YouTube Data API v3 e identifica conteudo infantil pelo campo
status.madeForKids (declarado pelo criador / definido pelo YouTube).

Etapas:
  1. search.list        -> descobre video_ids por palavras-chave x janelas de tempo
  2. videos.list        -> metadados (titulo, descricao, tags, stats, madeForKids...)
  3. channels.list      -> metadados dos canais
  4. commentThreads.list-> comentarios (so onde estiverem habilitados)
  5. exporta CSVs prontos para analise (pandas)

Custo de cota (padrao 10.000 unidades/dia):
  search.list = 100 | videos/channels/commentThreads/comments.list = 1

Uso:
  export YOUTUBE_API_KEY="sua_chave"
  python coletor_youtube_kids.py --months 6 --max-pages 3 --quota-limit 9500
  # pode rodar varias vezes (em dias diferentes): ele retoma de onde parou.
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

DB_PATH = "youtube_kids.db"
OUT_DIR = "dados_exportados"
FMT = "%Y-%m-%dT%H:%M:%SZ"



# ---------------------------------------------------------------- cota / API
class QuotaExhausted(Exception):
    pass


class Quota:
    COST = {"search": 100, "videos": 1, "channels": 1,
            "commentThreads": 1, "comments": 1}

    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def charge(self, kind):
        cost = self.COST[kind]
        if self.used + cost > self.limit:
            raise QuotaExhausted(f"Limite de cota atingido ({self.used}/{self.limit})")
        self.used += cost


def error_reason(e):
    try:
        return json.loads(e.content.decode())["error"]["errors"][0]["reason"]
    except Exception:
        return ""


def execute(request, kind, quota, retries=4):
    """Executa uma requisicao com controle de cota e retry em erros transitorios."""
    quota.charge(kind)
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            reason = error_reason(e)
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise QuotaExhausted("Cota diaria da API esgotada (retome amanha).")
            if status in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


# ------------------------------------------------------------------- banco
SCHEMA = """
CREATE TABLE IF NOT EXISTS search_hits(
    video_id TEXT, query TEXT, window_start TEXT, window_end TEXT, rank INTEGER,
    PRIMARY KEY(video_id, query, window_start));
CREATE TABLE IF NOT EXISTS searches_done(
    query TEXT, window_start TEXT, PRIMARY KEY(query, window_start));
CREATE TABLE IF NOT EXISTS videos(
    video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, description TEXT,
    published_at TEXT, tags TEXT, category_id TEXT, default_language TEXT,
    default_audio_language TEXT, duration TEXT, definition TEXT, caption TEXT,
    licensed_content INTEGER, view_count INTEGER, like_count INTEGER,
    comment_count INTEGER, made_for_kids INTEGER, self_declared_made_for_kids INTEGER,
    privacy_status TEXT, embeddable INTEGER, topic_categories TEXT,
    thumbnail_url TEXT, comments_status TEXT DEFAULT 'pending', collected_at TEXT);
CREATE TABLE IF NOT EXISTS channels(
    channel_id TEXT PRIMARY KEY, title TEXT, description TEXT, country TEXT,
    published_at TEXT, subscriber_count INTEGER, video_count INTEGER,
    view_count INTEGER, made_for_kids INTEGER, collected_at TEXT);
CREATE TABLE IF NOT EXISTS comments(
    comment_id TEXT PRIMARY KEY, video_id TEXT, parent_id TEXT,
    author_hash TEXT, text TEXT, like_count INTEGER,
    published_at TEXT, updated_at TEXT);
"""


def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def now_iso():
    return datetime.now(timezone.utc).strftime(FMT)


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None

def to_bool_int(x):
    if x is None:
        return None
    return int(bool(x))


def hash_author(author_id):
    """Pseudonimiza o autor (boa pratica de etica em pesquisa)."""
    if not author_id:
        return None
    return hashlib.sha256(author_id.encode()).hexdigest()[:16]


# ------------------------------------------------------------ janelas de tempo
def month_windows(n):
    """Ultimos n meses, mes a mes. Janelas contornam o limite (~500) de resultados por busca."""
    today = datetime.now(timezone.utc)
    y, m = today.year, today.month
    wins = []
    for _ in range(n):
        start = datetime(y, m, 1, tzinfo=timezone.utc)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = datetime(ny, nm, 1, tzinfo=timezone.utc)
        wins.append((start.strftime(FMT), end.strftime(FMT)))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return wins


# ----------------------------------------------------------------- etapa 1
def step_search(yt, con, quota, queries, windows, max_pages, region, lang, safe):
    for q in queries:
        for (start, end) in windows:
            done = con.execute(
                "SELECT 1 FROM searches_done WHERE query=? AND window_start=?",
                (q, start)).fetchone()
            if done:
                continue
            page_token, rank, n_hits = None, 0, 0
            for _ in range(max_pages):
                req = yt.search().list(
                    part="snippet", q=q, type="video", maxResults=50,
                    publishedAfter=start, publishedBefore=end,
                    regionCode=region, relevanceLanguage=lang,
                    safeSearch=safe, order="relevance", pageToken=page_token)
                resp = execute(req, "search", quota)
                for item in resp.get("items", []):
                    vid = item["id"].get("videoId")
                    if not vid:
                        continue
                    rank += 1
                    n_hits += 1
                    con.execute("INSERT OR IGNORE INTO search_hits VALUES (?,?,?,?,?)",
                                (vid, q, start, end, rank))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            con.execute("INSERT OR IGNORE INTO searches_done VALUES (?,?)", (q, start))
            con.commit()
            print(f"[search] '{q}' {start[:7]} -> {n_hits} videos | cota {quota.used}")


# ----------------------------------------------------------------- etapa 2
def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def step_videos(yt, con, quota):
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT video_id FROM search_hits "
        "WHERE video_id NOT IN (SELECT video_id FROM videos)")]
    print(f"[videos] {len(ids)} videos novos para detalhar")
    for batch in chunks(ids, 50):
        req = yt.videos().list(
            part="snippet,contentDetails,statistics,status,topicDetails",
            id=",".join(batch), maxResults=50)
        resp = execute(req, "videos", quota)
        found = set()
        for it in resp.get("items", []):
            sn, cd = it.get("snippet", {}), it.get("contentDetails", {})
            st, status = it.get("statistics", {}), it.get("status", {})
            topics = it.get("topicDetails", {}).get("topicCategories", [])
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("default") or {}).get("url")
            found.add(it["id"])
            con.execute(
                "INSERT OR REPLACE INTO videos VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["id"], sn.get("channelId"), sn.get("title"), sn.get("description"),
                 sn.get("publishedAt"), json.dumps(sn.get("tags", []), ensure_ascii=False),
                 sn.get("categoryId"), sn.get("defaultLanguage"),
                 sn.get("defaultAudioLanguage"), cd.get("duration"), cd.get("definition"),
                 cd.get("caption"), int(bool(cd.get("licensedContent"))),
                 to_int(st.get("viewCount")), to_int(st.get("likeCount")),
                 to_int(st.get("commentCount")),
                 to_bool_int(status.get("madeForKids")),
                 to_bool_int(status.get("selfDeclaredMadeForKids")),
                 status.get("privacyStatus"), int(bool(status.get("embeddable"))),
                 json.dumps(topics), thumb,
                 # Sem commentCount => comentarios desativados/ocultos (nao gasta cota)
                 "pending" if "commentCount" in st else "disabled_or_hidden",
                 now_iso()))
        # videos removidos/privados desde a busca: registra para nao reconsultar
        for vid in set(batch) - found:
            con.execute("INSERT OR IGNORE INTO videos(video_id, privacy_status, "
                        "comments_status, collected_at) VALUES (?,?,?,?)",
                        (vid, "unavailable", "unavailable", now_iso()))
        con.commit()
    print(f"[videos] ok | cota {quota.used}")


# ----------------------------------------------------------------- etapa 3
def step_channels(yt, con, quota):
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT channel_id FROM videos WHERE channel_id IS NOT NULL "
        "AND channel_id NOT IN (SELECT channel_id FROM channels)")]
    print(f"[channels] {len(ids)} canais novos")
    for batch in chunks(ids, 50):
        req = yt.channels().list(part="snippet,statistics,status",
                                 id=",".join(batch), maxResults=50)
        resp = execute(req, "channels", quota)
        for it in resp.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            con.execute(
                "INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?,?,?,?)",
                (it["id"], sn.get("title"), sn.get("description"), sn.get("country"),
                 sn.get("publishedAt"),
                 None if st.get("hiddenSubscriberCount") else to_int(st.get("subscriberCount")),
                 to_int(st.get("videoCount")), to_int(st.get("viewCount")),
                 int(bool(it.get("status", {}).get("madeForKids"))), now_iso()))
        con.commit()
    print(f"[channels] ok | cota {quota.used}")


# ----------------------------------------------------------------- etapa 4
def save_comment(con, cid, vid, parent, sn):
    con.execute(
        "INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?,?,?,?)",
        (cid, vid, parent,
         hash_author((sn.get("authorChannelId") or {}).get("value")),
         sn.get("textOriginal") or sn.get("textDisplay"),
         to_int(sn.get("likeCount")), sn.get("publishedAt"), sn.get("updatedAt")))


def fetch_all_replies(yt, con, quota, vid, parent_id):
    token = None
    while True:
        resp = execute(yt.comments().list(part="snippet", parentId=parent_id,
                                          maxResults=100, textFormat="plainText",
                                          pageToken=token), "comments", quota)
        for c in resp.get("items", []):
            save_comment(con, c["id"], vid, parent_id, c["snippet"])
        token = resp.get("nextPageToken")
        if not token:
            break


def step_comments(yt, con, quota, max_comment_pages, full_replies):
    vids = [r[0] for r in con.execute(
        "SELECT video_id FROM videos WHERE comments_status='pending' "
        "ORDER BY comment_count DESC")]
    print(f"[comments] {len(vids)} videos com comentarios habilitados")
    for vid in vids:
        status, token = "ok", None
        try:
            for _ in range(max_comment_pages):
                resp = execute(yt.commentThreads().list(
                    part="snippet,replies", videoId=vid, maxResults=100,
                    order="relevance", textFormat="plainText", pageToken=token),
                    "commentThreads", quota)
                for th in resp.get("items", []):
                    top = th["snippet"]["topLevelComment"]
                    save_comment(con, top["id"], vid, None, top["snippet"])
                    replies = th.get("replies", {}).get("comments", [])
                    for r in replies:
                        save_comment(con, r["id"], vid, top["id"], r["snippet"])
                    if full_replies and th["snippet"].get("totalReplyCount", 0) > len(replies):
                        fetch_all_replies(yt, con, quota, vid, top["id"])
                token = resp.get("nextPageToken")
                if not token:
                    break
        except HttpError as e:
            reason = error_reason(e)
            status = "disabled" if reason == "commentsDisabled" else f"error:{reason or e.resp.status}"
        con.execute("UPDATE videos SET comments_status=? WHERE video_id=?", (status, vid))
        con.commit()
    print(f"[comments] ok | cota {quota.used}")


# ----------------------------------------------------------------- etapa 5
def export_csv(con):
    os.makedirs(OUT_DIR, exist_ok=True)
    tables = {
        "videos": "SELECT v.*, (SELECT GROUP_CONCAT(DISTINCT query) FROM search_hits s "
                  "WHERE s.video_id=v.video_id) AS queries FROM videos v "
                  "WHERE v.privacy_status!='unavailable'",
        "channels": "SELECT * FROM channels",
        "comments": "SELECT * FROM comments",
        "search_hits": "SELECT * FROM search_hits",
    }
    for name, sql in tables.items():
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        path = os.path.join(OUT_DIR, f"{name}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur)
        print(f"[export] {path}")
    # resumo para a secao "volume" do relatorio
    n = lambda sql: con.execute(sql).fetchone()[0]
    print("\n===== RESUMO DA COLETA =====")
    print("Videos:", n("SELECT COUNT(*) FROM videos WHERE privacy_status!='unavailable'"))
    print("  made_for_kids=1:", n("SELECT COUNT(*) FROM videos WHERE made_for_kids=1"))
    print("  comentarios habilitados:", n("SELECT COUNT(*) FROM videos WHERE comments_status='ok'"))
    print("Canais:", n("SELECT COUNT(*) FROM channels"))
    print("Comentarios:", n("SELECT COUNT(*) FROM comments"))


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Coletor YouTube (conteudo infantil)")
    ap.add_argument("--api-key", default=os.environ.get("YOUTUBE_API_KEY"))
    ap.add_argument("--queries-file",required=True,help="arquivo .txt com uma palavra-chave por linha")
    ap.add_argument("--months", type=int, default=6, help="quantos meses para tras")
    ap.add_argument("--max-pages", type=int, default=2, help="paginas de busca (50 videos/pagina)")
    ap.add_argument("--region", default="BR")
    ap.add_argument("--lang", default="pt")
    ap.add_argument("--safe-search", default="none", choices=["none", "moderate", "strict"],
                    help="use 'none' para nao esconder justamente o conteudo que se quer estudar")
    ap.add_argument("--quota-limit", type=int, default=9500)
    ap.add_argument("--comment-pages", type=int, default=3, help="paginas de comentarios por video (100/pag)")
    ap.add_argument("--full-replies", action="store_true", help="busca todas as respostas dos comentarios")
    ap.add_argument("--skip-search", action="store_true", help="pula a etapa 1 (so detalha o que ja foi achado)")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Defina a variavel YOUTUBE_API_KEY ou use --api-key")

    with open(args.queries_file, encoding="utf-8") as f:
        queries = [l.strip() for l in f if l.strip()]

    if not queries: 
        sys.exit("O arquivo de consultas está vazio.")

    yt = build("youtube", "v3", developerKey=args.api_key, cache_discovery=False)
    con = db_connect()
    quota = Quota(args.quota_limit)

    try:
        if not args.skip_search:
            step_search(yt, con, quota, queries, month_windows(args.months),
                        args.max_pages, args.region, args.lang, args.safe_search)
        step_videos(yt, con, quota)
        step_channels(yt, con, quota)
        step_comments(yt, con, quota, args.comment_pages, args.full_replies)
    except QuotaExhausted as e:
        print(f"\n[!] {e}\nOs dados ja coletados foram salvos. Rode novamente depois para continuar.")
    finally:
        con.commit()
        export_csv(con)
        print(f"\nCota usada nesta execucao: {quota.used}")


if __name__ == "__main__":
    main()
