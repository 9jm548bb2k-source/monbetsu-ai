"""Monbetsu race result collector for GitHub Actions."""

import json
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

START_DATE = date(2023, 1, 1)
END_DATE = date.fromisoformat(
    os.environ.get("END_DATE", date.today().isoformat())
)

MAX_RUN_MINUTES = int(os.environ.get("MAX_RUN_MINUTES", "48"))
WAIT_SECONDS = float(os.environ.get("WAIT_SECONDS", "0.6"))

ROOT = Path(__file__).resolve().parent

DATA = ROOT / "results.csv"
LOG = ROOT / "v6_audit.csv"
CURSOR = ROOT / "v6_cursor.json"
STATUS = ROOT / "collection_status.json"

BASE = "https://www.hokkaidokeiba.net/raceinfo/"

KEYS = ["開催日", "レース", "馬番"]

COLUMNS = [
    "開催日",
    "レース",
    "馬番",
    "馬名",
    "着順",
    "騎手",
    "調教師",
    "枠番",
    "性齢",
    "斤量",
    "馬体重",
    "走破タイム",
    "着差",
    "単勝オッズ",
    "複勝オッズ範囲",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ResearchCollector/1.0)"
})


def get_soup(url):
    last_error = None

    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=25)
            response.raise_for_status()

            time.sleep(
                WAIT_SECONDS + random.uniform(0, 0.25)
            )

            return BeautifulSoup(
                response.content,
                "html.parser"
            )

        except requests.RequestException as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(
        f"通信失敗: {url}: {last_error}"
    )


def text(cell):
    return cell.get_text(
        " ", strip=True
    ).replace("\u3000", " ")


def normalize(value):
    return re.sub(
        r"\s+",
        "",
        str(value)
    ).replace("　", "")


def expand_table(table):
    pending = {}
    output = []

    for tr in table.select("tr"):
        row = []
        col = 0

        def fill():
            nonlocal col

            while col in pending:
                value, left = pending[col]
                row.append(value)

                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (value, left - 1)

                col += 1

        fill()

        for cell in tr.find_all(
            ["td", "th"],
            recursive=False
        ):
            fill()

            value = text(cell)

            colspan = int(
                cell.get("colspan", 1)
            )
            rowspan = int(
                cell.get("rowspan", 1)
            )

            for _ in range(colspan):
                row.append(value)

                if rowspan > 1:
                    pending[col] = (
                        value,
                        rowspan - 1
                    )

                col += 1

            fill()

        fill()

        if row:
            output.append(row)

    return output


def find_table(soup, required):
    required = [
        normalize(word)
        for word in required
    ]

    for table in soup.select("table"):
        rows = expand_table(table)

        if not rows:
            continue

        for index, row in enumerate(
            rows[:5]
        ):
            header = [
                normalize(value)
                for value in row
            ]

            combined = "".join(header)

            if all(
                word in combined
                for word in required
            ):
                return rows[index:]

    raise ValueError(
        f"表が見つかりません: {required}"
    )


def table_records(rows):
    if not rows:
        return []

    header = [
        normalize(value)
        for value in rows[0]
    ]

    if (
        "負担" in header
        and "重量" in header
    ):
        index = header.index("負担")
        header[index] = "斤量"
        header.pop(
            header.index("重量")
        )

    records = []

    for index, row in enumerate(
        rows[1:], 1
    ):
        if len(row) != len(header):
            raise ValueError(
                f"行列不一致: {index}, "
                f"header={header}, row={row}"
            )

        records.append(
            dict(zip(header, row))
        )

    return records


def race_links(day):
    stamp = day.strftime("%Y%m%d")

    soup = get_soup(
        BASE + f"index.php?p_day={stamp}"
    )

    numbers = set()

    for link in soup.find_all(
        "a", href=True
    ):
        url = urljoin(
            BASE,
            link["href"]
        )

        query = parse_qs(
            urlparse(url).query
        )

        if (
            "seiseki.php" in url
            and query.get(
                "p_day", [""]
            )[0] == stamp
        ):
            try:
                numbers.add(
                    int(
                        query["p_rno"][0]
                    )
                )
            except (
                KeyError,
                ValueError
            ):
                pass

    return sorted(numbers)


def race_tables(day, number):
    query = (
        f"p_day={day.strftime('%Y%m%d')}"
        f"&p_rno={number:03d}"
    )

    result_page = get_soup(
        BASE + "seiseki.php?" + query
    )

    odds_page = get_soup(
        BASE + "odds/tanhuku.php?" + query
    )

    results = find_table(
        result_page,
        ["着順", "馬番", "馬名"]
    )

    odds = find_table(
        odds_page,
        ["馬番", "単勝オッズ"]
    )

    return results, odds


def number(value):
    try:
        return int(
            str(value).strip()
        )
    except (
        TypeError,
        ValueError
    ):
        return None


def decimal(value):
    try:
        return float(
            str(value).replace(
                ",", ""
            )
        )
    except (
        TypeError,
        ValueError
    ):
        return None


def parse_race(day, race):
    result_rows, odds_rows = (
        race_tables(day, race)
    )

    results = table_records(
        result_rows
    )

    odds = table_records(
        odds_rows
    )

    if not results:
        raise ValueError(
            "結果データが0行"
        )

    odds_by_number = {
        number(
            row.get("馬番")
        ): row
        for row in odds
    }

    output = []

    for record in results:
        horse_number = number(
            record.get("馬番")
        )

        if horse_number is None:
            continue

        odd = odds_by_number.get(
            horse_number, {}
        )

        output.append({
            "開催日": day.isoformat(),
            "レース": race,
            "馬番": horse_number,
            "馬名": record.get("馬名"),
            "着順": number(
                record.get("着順")
            ),
            "騎手": record.get("騎手"),
            "調教師": record.get("調教師"),
            "枠番": record.get("枠番"),
            "性齢": record.get("性齢"),
            "斤量": record.get(
                "斤量",
                record.get("負担重量")
            ),
            "馬体重": record.get(
                "馬体重"
            ),
            "走破タイム": record.get(
                "タイム"
            ),
            "着差": record.get(
                "着差"
            ),
            "単勝オッズ": decimal(
                odd.get("単勝オッズ")
            ),
            "複勝オッズ範囲": odd.get(
                "複勝オッズ"
            ),
        })

    if not output:
        raise ValueError(
            "馬番の解析に失敗"
        )

    return output


def atomic_csv(frame, path):
    temporary = path.with_name(
        path.name + ".tmp"
    )

    frame.to_csv(
        temporary,
        index=False,
        encoding="utf-8-sig"
    )

    os.replace(
        temporary,
        path
    )


def atomic_json(data, path):
    temporary = path.with_name(
        path.name + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    os.replace(
        temporary,
        path
    )


def load_cursor():
    try:
        data = json.loads(
            CURSOR.read_text(
                encoding="utf-8"
            )
        )

        return date.fromisoformat(
            data["next_date"]
        )

    except (
        FileNotFoundError,
        KeyError,
        ValueError,
        json.JSONDecodeError
    ):
        return START_DATE


def main():
    if DATA.exists():
        old = pd.read_csv(
            DATA,
            encoding="utf-8-sig",
            low_memory=False
        )
    else:
        old = pd.DataFrame(
            columns=COLUMNS
        )

    missing_columns = [
        key
        for key in KEYS
        if key not in old.columns
    ]

    if missing_columns:
        raise ValueError(
            "既存CSVの必須列がありません: "
            + str(missing_columns)
        )

    all_rows = old.to_dict(
        "records"
    )

    if LOG.exists():
        audit = pd.read_csv(
            LOG,
            dtype=str
        ).fillna("")
    else:
        audit = pd.DataFrame(
            columns=[
                "date",
                "race",
                "status",
                "count",
                "message"
            ]
        )

    completed = set()

    for row in all_rows:
        try:
            completed.add((
                str(
                    row["開催日"]
                )[:10],
                int(
                    row["レース"]
                )
            ))
        except (
            KeyError,
            TypeError,
            ValueError
        ):
            pass

    cursor = max(
        load_cursor(),
        START_DATE
    )

    if cursor > END_DATE:
        cursor = max(
            START_DATE,
            END_DATE - timedelta(
                days=2
            )
        )

        completed = {
            key
            for key in completed
            if key[0] < cursor.isoformat()
        }

    start_cursor = cursor
    clock = time.monotonic()
    cutoff = MAX_RUN_MINUTES * 60

    added = 0
    checked = 0
    issues = []

    def checkpoint(next_day):
        nonlocal all_rows

        frame = pd.DataFrame(
            all_rows
        )

        if not frame.empty:
            frame = frame.drop_duplicates(
                KEYS,
                keep="last"
            )

            atomic_csv(
                frame,
                DATA
            )

            all_rows = frame.to_dict(
                "records"
            )

        atomic_csv(
            audit,
            LOG
        )

        atomic_json(
            {
                "next_date":
                next_day.isoformat()
            },
            CURSOR
        )

    print(
        "既存行数:",
        len(all_rows),
        "開始日:",
        cursor,
        "終了日:",
        END_DATE,
        flush=True
    )

    day = cursor

    while day <= END_DATE:
        if (
            time.monotonic() - clock
            >= cutoff
        ):
            break

        checked += 1

        try:
            races = race_links(day)

        except Exception as exc:
            issues.append({
                "date": day.isoformat(),
                "race": "一覧",
                "error": repr(exc)
            })

            print(
                "開催一覧取得失敗:",
                day,
                repr(exc),
                flush=True
            )

            day += timedelta(days=1)
            checkpoint(day)
            continue

        remaining = [
            race
            for race in races
            if (
                day.isoformat(),
                race
            ) not in completed
        ]

        if remaining:
            print(
                "開催:",
                day,
                "未取得:",
                len(remaining),
                flush=True
            )

        for race in remaining:
            if (
                time.monotonic() - clock
                >= cutoff
            ):
                break

            try:
                rows = parse_race(
                    day,
                    race
                )

                all_rows = [
                    row
                    for row in all_rows
                    if not (
                        str(
                            row.get("開催日")
                        ) == day.isoformat()
                        and str(
                            row.get("レース")
                        ) == str(race)
                    )
                ]

                all_rows.extend(
                    rows
                )

                audit.loc[
                    len(audit)
                ] = [
                    day.isoformat(),
                    str(race),
                    "ok",
                    str(len(rows)),
                    ""
                ]

                completed.add((
                    day.isoformat(),
                    race
                ))

                added += 1

            except Exception as exc:
                issues.append({
                    "date": day.isoformat(),
                    "race": race,
                    "error": repr(exc)
                })

                audit.loc[
                    len(audit)
                ] = [
                    day.isoformat(),
                    str(race),
                    "error",
                    "0",
                    repr(exc)[:400]
                ]

                print(
                    "取得失敗:",
                    day,
                    race,
                    repr(exc)[:150],
                    flush=True
                )

        day += timedelta(
            days=1
        )

        checkpoint(day)

    pending = day <= END_DATE

    progress = (
        day > start_cursor
        or added > 0
    )

    status = {
        "updated_at_utc":
            datetime.now(
                timezone.utc
            ).isoformat(),
        "next_date":
            day.isoformat(),
        "end_date":
            END_DATE.isoformat(),
        "historical_pending":
            pending,
        "made_progress":
            progress,
        "continue_automatically":
            pending and progress,
        "added_races":
            added,
        "checked_days":
            checked,
        "total_rows":
            len(all_rows),
        "errors":
            len(issues),
        "issues":
            issues[:100]
    }

    atomic_json(
        status,
        STATUS
    )

    print(
        "今回追加レース:",
        added,
        "確認日数:",
        checked,
        "総行数:",
        len(all_rows),
        "問題:",
        len(issues),
        flush=True
    )

    print(
        "自動継続:",
        status[
            "continue_automatically"
        ],
        "次回開始日:",
        day,
        flush=True
    )


if __name__ == "__main__":
    main()