#!/usr/bin/env python3
"""Скачивание инструкций из карточек ГРЛС по списку адресов карточек, GUID или номеров РУ.

Как это устроено (по коду страницы grls.rosminzdrav.ru):
  0. Поиск карточки по номеру РУ (или любому тексту из строки поиска):
     POST /GRLS.aspx с полями формы (viewstate/eventvalidation из предварительного
     GET + ctl00$plate$txtRegNm=<номер РУ> + ctl00$plate$bSeek=Найти). В ответе —
     таблица #ctl00_plate_gr, строки которой несут onclick="det('<guid>', <isFS>)".
     Капча на этой странице (canvas, jquery-captcha) — чисто клиентская проверка:
     ответ сверяется в браузере и никуда не отправляется, сервер его не видит,
     так что обычный постбэк без браузера проходит и без «решения» капчи.
  1. Карточка:  GET /Grls_View_v2.aspx?routingGuid=<guid>  -> HTML + cookie сессии.
     В HTML: #ctl00_plate_RegNr (номер РУ), #ctl00_plate_hfIdReg (внутренний id),
     #ctl00_plate_TradeNmR (торговое название) и скрытое поле с id, содержащим «hfInstructionModel».
  2. Список файлов инструкции — JSON  {"Sources":[{"Instructions":[{"Images":[{"Url","Label"}],"Label"}]}]}.
     Если скрытое поле заполнено — берём его. Если пустое — тот же JSON отдаёт
     POST /GRLS_View_V2.aspx/AddInstrImg  с телом {"regNumber": "<РУ>", "idReg": "<id>"}
     (с cookie из шага 1); ответ вида {"d": "<строка с JSON>"}.
  3. Файлы: GET <сайт><Url>. Url бывает с обратными слэшами и кириллицей:
     \\InstrImg\\0001458571\\0000610248\\ЛС-000574[2017]_0.pdf  — их нужно превратить в «/» и
     закодировать. У свежих карточек имя файла — GUID.pdf.
  4. В подписи файла бывает номер изменения и год: «Изм. № 0, ЛС-000574, 2017» (0 — исходная версия).

Использование:
    python instructions.py cards.txt --out kb/raw/instructions

cards.txt — по одному адресу карточки на строку или просто GUID; строки с # игнорируются.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

CARD_URL = "https://grls.rosminzdrav.ru/Grls_View_v2.aspx?routingGuid={guid}"
FS_CARD_URL = "https://grls.rosminzdrav.ru/Grls_viewFS_v2.aspx?routingGuid={guid}"
SEARCH_URL = "https://grls.rosminzdrav.ru/GRLS.aspx"
INSTR_PATH = "/GRLS_View_V2.aspx/AddInstrImg"
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
DET_RE = re.compile(r"det\('([0-9a-fA-F-]{36})'\s*,\s*(\d)\)")
AMEND_RE = re.compile(r"Изм\.?\s*№\s*(\d+)")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def search_by_regnum(sess: requests.Session, reg_num: str) -> list[dict]:
    """Ищет карточки на странице поиска ГРЛС по номеру РУ (точный текстовый поиск сайта).

    Капча на странице поиска — не серверная проверка: это открытая JS-библиотека
    jquery-captcha, рисующая код на canvas и сверяющая ответ тут же в браузере;
    ответ никуда не отправляется и сервером не проверяется. Поэтому обычный
    POST-постбэк формы без эмуляции браузера работает и без «решения» капчи.
    """
    r1 = sess.get(SEARCH_URL, timeout=30)
    soup = BeautifulSoup(r1.content, "html.parser")

    def val(el_id: str) -> str:
        el = soup.find(id=el_id)
        return (el.get("value") or "") if el else ""

    data = {
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        "__VIEWSTATE": val("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": val("__VIEWSTATEGENERATOR"),
        "__VIEWSTATEENCRYPTED": "",
        "__EVENTVALIDATION": val("__EVENTVALIDATION"),
        "ctl00$plate$isFS": "0",
        "ctl00$plate$txtRegNm": reg_num,
        "ctl00$plate$txtMNN": "", "ctl00$plate$LF": "", "ctl00$plate$txtTorg": "",
        "ctl00$plate$ownName": "", "ctl00$plate$txtMnf": "", "ctl00$plate$txtMnfCountry": "",
        "ctl00$plate$hfRegType": val("ctl00_plate_hfRegType") or "1,6",
        "ctl00$plate$txtRecordOnPageCount": "20",
        "ctl00$plate$bSeek": "Найти",
    }
    r2 = sess.post(SEARCH_URL, data=data, timeout=30, headers={"Referer": SEARCH_URL, "Origin": origin(SEARCH_URL)})
    soup2 = BeautifulSoup(r2.content, "html.parser")
    grid = soup2.find(id=re.compile("ctl00_plate_gr"))
    results = []
    for tr in (grid.find_all("tr")[1:] if grid else []):
        m = DET_RE.search(tr.get("onclick", ""))
        if not m:
            continue
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        results.append({
            "guid": m.group(1), "is_fs": m.group(2) == "1",
            "trade_name": tds[1] if len(tds) > 1 else "",
            "inn": tds[2] if len(tds) > 2 else "",
            "ru_num": tds[6] if len(tds) > 6 else "",
        })
    return results


def resolve_card_url(sess: requests.Session, query: str) -> str:
    """Находит карточку препарата по тексту (обычно — номеру РУ) через поиск сайта."""
    results = search_by_regnum(sess, query)
    if not results:
        raise ValueError(f"поиск по «{query}» не дал результатов")
    exact = [r for r in results if r["ru_num"] == query]
    chosen = exact[0] if exact else results[0]
    template = FS_CARD_URL if chosen["is_fs"] else CARD_URL
    return template.format(guid=chosen["guid"])


def card_url(sess: requests.Session, line: str) -> str:
    line = line.strip()
    if GUID_RE.match(line):
        return CARD_URL.format(guid=line)
    if line.startswith("http://") or line.startswith("https://"):
        return line
    return resolve_card_url(sess, line)


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _get(d: dict, name: str):
    """Ключи ожидаются в PascalCase (как в коде страницы); на всякий случай и в нижнем регистре."""
    return d.get(name) if name in d else d.get(name.lower())


def parse_card(html: bytes | str) -> dict:
    """Достаёт из HTML карточки номер РУ, id, название и (если есть) JSON-модель инструкций.

    Принимает байты: кодировку (utf-8 или windows-1251) BeautifulSoup определяет по странице;
    response.text у requests без charset в заголовке читает её как latin-1 и портит кириллицу.
    """
    soup = BeautifulSoup(html, "html.parser")

    def val(el_id: str) -> str:
        el = soup.find(id=el_id)
        return ((el.get("value") or el.get_text(strip=True)) if el else "").strip()

    out = {"ru_num": val("ctl00_plate_RegNr"), "id_reg": val("ctl00_plate_hfIdReg"),
           "trade_name": val("ctl00_plate_TradeNmR"), "model": None, "note": ""}
    field = soup.find(id=re.compile("hfInstructionModel"))
    if field is None:
        out["note"] = "в карточке нет поля hfInstructionModel"
    elif (field.get("value") or "").strip():
        out["model"] = json.loads(field["value"])
    else:
        out["note"] = "поле hfInstructionModel пустое"
    return out


def fetch_model_fallback(sess: requests.Session, site: str, ru_num: str, id_reg: str, referer: str) -> dict:
    """Если поле пустое: тот же JSON отдаёт page-method AddInstrImg (нужны cookie, полученные при GET карточки)."""
    if not ru_num or not id_reg:
        raise ValueError("нет номера РУ или idReg для запроса AddInstrImg")
    r = sess.post(site + INSTR_PATH, json={"regNumber": ru_num, "idReg": id_reg},
                  headers={"Referer": referer}, timeout=60)
    d = r.json().get("d")
    return json.loads(d) if isinstance(d, str) else (d or {})


def files_from_model(model: dict) -> list[dict]:
    files = []
    for si, source in enumerate(_get(model, "Sources") or []):
        for ii, instr in enumerate(_get(source, "Instructions") or []):
            for ji, img in enumerate(_get(instr, "Images") or []):
                url = _get(img, "Url")
                if not url:
                    continue
                label = _get(img, "Label") or ""
                m = AMEND_RE.search(label)
                years = YEAR_RE.findall(label)
                files.append({
                    "source": si, "instruction": ii, "image": ji,
                    "source_name": _get(source, "SourceName") or "",
                    "instruction_label": _get(instr, "Label") or "",
                    "url": url, "label": label,
                    "amendment_no": int(m.group(1)) if m else None,
                    "year": int(years[-1]) if years else None,
                    # «Изм. № 0» — исходная версия; больше нуля — изменения; иначе не знаем
                    "role": "unknown" if not m else ("main" if int(m.group(1)) == 0 else "amendment"),
                })
    return files


def normalize_file_url(card: str, raw: str) -> str:
    """Обратные слэши -> «/», путь закодировать (кириллица, скобки), собрать абсолютный адрес."""
    absolute = urljoin(card, raw.replace("\\", "/"))
    p = urlsplit(absolute)
    return urlunsplit((p.scheme, p.netloc, quote(unquote(p.path), safe="/"), p.query, ""))


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE).strip("_") or "x"


def process_card(sess: requests.Session, url: str, out: Path) -> dict:
    r = sess.get(url, timeout=60)
    info = parse_card(r.content)
    model, note = info["model"], info["note"]
    if model is None:
        try:
            model = fetch_model_fallback(sess, origin(url), info["ru_num"], info["id_reg"], url)
            note = (note + "; " if note else "") + "список файлов получен через AddInstrImg"
        except Exception as e:  # noqa: BLE001 — сообщаем в manifest и идём к следующей карточке
            model, note = {}, f"{note}; AddInstrImg не сработал: {e}"
    folder = out / safe_name(info["ru_num"] or urlsplit(url).query or "card")
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files_from_model(model):
        file_url = normalize_file_url(url, f["url"])
        base = Path(unquote(urlsplit(file_url).path)).name or "file"
        path = folder / f"{f['source']:02d}_{f['instruction']:02d}_{f['image']:02d}_{safe_name(base)}"
        if not path.exists():
            fr = sess.get(file_url, timeout=120)
            path.write_bytes(fr.content)
        data = path.read_bytes()
        saved.append({**f, "file_url": file_url, "file": path.name, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "is_pdf": data[:5] == b"%PDF-"})
    if not saved and not note:
        note = "в модели нет файлов"
    manifest = {"card_url": url, "ru_num": info["ru_num"], "trade_name": info["trade_name"],
                "note": note, "files": saved}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cards", type=Path, help="файл со списком адресов карточек или GUID")
    ap.add_argument("--out", type=Path, default=Path("kb/raw/instructions"))
    a = ap.parse_args()

    lines = [l.strip() for l in a.cards.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
    a.out.mkdir(parents=True, exist_ok=True)

    for i, line in enumerate(lines, 1):
        try:
            url = card_url(sess, line)
        except Exception as e:  # noqa: BLE001 — не нашли карточку, идём к следующей строке
            print(f"[{i}/{len(lines)}] {line}: не удалось найти карточку ({e})")
            continue
        m = process_card(sess, url, a.out)
        print(f"[{i}/{len(lines)}] {m['ru_num'] or url}: файлов {len(m['files'])}"
              + (f" ({m['note']})" if m["note"] else ""))


if __name__ == "__main__":
    main()