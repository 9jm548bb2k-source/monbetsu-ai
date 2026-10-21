"""Monbetsu result collector. Preserve and repair legacy CSV column encodings."""
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

START_DATE = date.fromisoformat(os.getenv('START_DATE', '2023-01-01'))
END_DATE = date.fromisoformat(os.getenv('END_DATE', date.today().isoformat()))
MAX_RUN_MINUTES = int(os.getenv('MAX_RUN_MINUTES', '55'))
WAIT_SECONDS = float(os.getenv('WAIT_SECONDS', '0.8'))

ROOT = Path(__file__).resolve().parent
DATA, LOG, CURSOR = [
    ROOT / name
    for name in ('results.csv', 'v6_audit.csv', 'v6_cursor.json')
]
STATUS = ROOT / 'collection_status.json'

BASE = 'https://www.hokkaidokeiba.net/raceinfo/'
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (compatible; ResearchCollector/1.0)'
})

KEYS = ['開催日', 'レース', '馬番']

COLUMNS = [
    '開催日', 'レース', '馬番', '馬名', '着順',
    '騎手', '調教師', '枠番', '性齢', '斤量',
    '馬体重', '走破タイム', '着差',
    '単勝オッズ', '複勝オッズ範囲'
]


def unmojibake(value):
    if not isinstance(value, str):
        return value

    text = value.lstrip('\ufeff')

    for _ in range(2):
        try:
            fixed = text.encode('latin1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            break

        if fixed == text:
            break

        text = fixed

    return text


def load_results():
    if not DATA.exists():
        return pd.DataFrame(columns=COLUMNS)

    frame = pd.read_csv(
        DATA,
        encoding='utf-8-sig',
        low_memory=False,
        dtype={'馬番': str}
    )

    frame.columns = [
        unmojibake(str(col))
        for col in frame.columns
    ]

    if any(key not in frame.columns for key in KEYS):
        raise ValueError(
            f'CSVの必須列が見つかりません: {list(frame.columns)}。'
            '既存データは変更していません。'
        )

    for col in ['開催日', '馬名', '騎手', '調教師']:
        if col in frame:
            frame[col] = frame[col].map(unmojibake)

    return frame


def get_soup(url):
    last = None

    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=25)
            response.raise_for_status()

            time.sleep(
                WAIT_SECONDS + random.uniform(0, 0.25)
            )

            return BeautifulSoup(
                response.content,
                'html.parser'
            )

        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(
        f'通信失敗 {url}: {last}'
    )


def txt(cell):
    return cell.get_text(
        ' ',
        strip=True
    ).replace('\u3000', ' ')


def expand_table(table):
    pending, output = {}, []

    for tr in table.select('tr'):
        row, col = [], 0

        def fill():
            nonlocal col

            while col in pending:
                value, left = pending[col]
                row.append(value)

                if left == 1:
                    del pending[col]
                else:
                    pending[col] = (value, left - 1)

                col += 1

        fill()

        for cell in tr.find_all(
            ['td', 'th'],
            recursive=False
        ):
            fill()

            value = txt(cell)
            rowspan = int(cell.get('rowspan', 1))
            colspan = int(cell.get('colspan', 1))

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


def race_links(day):
    stamp = day.strftime('%Y%m%d')

    soup = get_soup(
        BASE + f'index.php?p_day={stamp}'
    )

    numbers = set()

    for anchor in soup.find_all('a', href=True):
        url = urljoin(BASE, anchor['href'])
        query = parse_qs(urlparse(url).query)

        if (
            'seiseki.php' in url
            and query.get('p_day', [''])[0] == stamp
        ):
            try:
                numbers.add(
                    int(query['p_rno'][0])
                )
            except (KeyError, ValueError):
                pass

    return sorted(numbers)


def race_tables(day, num):
    query = (
        f'p_day={day.strftime("%Y%m%d")}'
        f'&p_rno={num:03d}'
    )

    result_page = get_soup(
        BASE + 'seiseki.php?' + query
    )

    odds_page = get_soup(
        BASE + 'odds/tanhuku.php?' + query
    )

    def choose(soup, words):
        for table in soup.select('table'):
            header = ' '.join(
                txt(cell)
                for cell in table.select('th')
            )

            if all(
                word in header
                for word in words
            ):
                return expand_table(table)

        raise ValueError(
            f'表が見つかりません: {words}'
        )

    return (
        choose(
            result_page,
            ['着順', '馬番', '馬名']
        ),
        choose(
            odds_page,
            ['馬番', '単勝オッズ']
        )
    )


def table_records(rows):
    if not rows:
        raise ValueError('空のテーブル')

    header = [
        re.sub(r'\s+', '', item)
        for item in rows[0]
    ]

    if '負担' in header and '重量' in header:
        index = header.index('負担')
        header[index] = '斤量'
        header.pop(
            header.index('重量')
        )

    records = []

    for index, row in enumerate(
        rows[1:],
        1
    ):
        if len(row) != len(header):
            raise ValueError(
                f'行列不一致 row={index} '
                f'header={header} row={row}'
            )

        records.append(
            dict(zip(header, row))
        )

    return records


def parse_race(day, num):
    result_rows, odds_rows = race_tables(
        day,
        num
    )

    results = table_records(result_rows)
    odds = table_records(odds_rows)

    def number(value):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def decimal(value):
        try:
            return float(
                str(value).replace(',', '')
            )
        except (TypeError, ValueError):
            return None

    odds_by_no = {
        number(record.get('馬番')): record
        for record in odds
    }

    if not results:
        raise ValueError('結果0行')

    if len(odds_by_no) < len(results) - 2:
        raise ValueError(
            f'オッズ行不足 '
            f'結果{len(results)} '
            f'オッズ{len(odds_by_no)}'
        )

    output = []

    for record in results:
        horse = number(
            record.get('馬番')
        )

        if horse is None:
            continue

        odd = odds_by_no.get(
            horse,
            {}
        )

        output.append({
            '開催日': day.isoformat(),
            'レース': num,
            '馬番': horse,
            '馬名': record.get('馬名'),
            '着順': number(
                record.get('着順')
            ),
            '騎手': record.get('騎手'),
            '調教師': record.get('調教師'),
            '枠番': record.get('枠番'),
            '性齢': record.get('性齢'),
            '斤量': record.get(
                '斤量',
                record.get('負担重量')
            ),
            '馬体重': record.get('馬体重'),
            '走破タイム': record.get('タイム'),
            '着差': record.get('着差'),
            '単勝オッズ': decimal(
                odd.get('単勝オッズ')
            ),
            '複勝オッズ範囲': odd.get(
                '複勝オッズ'
            )
        })

    if not output:
        raise ValueError(
            '馬番の解析失敗'
        )

    return output


def atomic_csv(frame, path):
    temporary = path.with_name(
        path.name + '.tmp'
    )

    frame.to_csv(
        temporary,
        index=False,
        encoding='utf-8-sig'
    )

    os.replace(
        temporary,
        path
    )


def main():
    old = load_results()
    rows = old.to_dict('records')

    if LOG.exists():
        audit = pd.read_csv(
            LOG,
            dtype=str
        ).fillna('')
    else:
        audit = pd.DataFrame(
            columns=[
                'date',
                'race',
                'status',
                'count',
                'message'
            ]
        )

    completed = set()

    for row in rows:
        try:
            completed.add((
                str(row['開催日'])[:10],
                int(row['レース'])
            ))
        except (
            KeyError,
            ValueError,
            TypeError
        ):
            pass

    for record in audit.itertuples(
        index=False
    ):
        try:
            key = (
                str(record.date),
                int(record.race)
            )

            if record.status == 'ok':
                completed.add(key)

            elif record.status == 'error':
                completed.discard(key)

        except (
            ValueError,
            AttributeError
        ):
            pass

    try:
        cursor = date.fromisoformat(
            json.loads(
                CURSOR.read_text()
            )['next_date']
        )
    except (
        FileNotFoundError,
        KeyError,
        ValueError,
        json.JSONDecodeError
    ):
        cursor = START_DATE

    cursor = max(
        cursor,
        START_DATE
    )

    if cursor > END_DATE:
        cursor = max(
            START_DATE,
            END_DATE - timedelta(days=2)
        )

        completed = {
            key
            for key in completed
            if key[0] < cursor.isoformat()
        }

    initial_cursor = cursor
    clock = time.monotonic()
    cutoff = MAX_RUN_MINUTES * 60

    added = 0
    checked = 0
    issues = []

    def checkpoint(next_day):
        nonlocal rows

        frame = pd.DataFrame(rows)

        if not frame.empty:
            for col in KEYS:
                if col not in frame.columns:
                    raise ValueError(
                        f'必須列がありません: {col}'
                    )

            frame = frame.drop_duplicates(
                KEYS,
                keep='last'
            )

            atomic_csv(
                frame,
                DATA
            )

            rows = frame.to_dict('records')

        atomic_csv(
            audit,
            LOG
        )

        temp = CURSOR.with_suffix(
            '.json.tmp'
        )

        temp.write_text(
            json.dumps(
                {
                    'next_date':
                    next_day.isoformat()
                },
                ensure_ascii=False
            ),
            encoding='utf-8'
        )

        os.replace(
            temp,
            CURSOR
        )

    print(
        '既存行数:',
        len(rows),
        '開始日:',
        cursor,
        '終了日:',
        END_DATE,
        flush=True
    )

    day = cursor

    while day <= END_DATE:
        if time.monotonic() - clock >= cutoff:
            break

        checked += 1

        try:
            nums = race_links(day)

        except Exception as exc:
            issues.append((
                str(day),
                '一覧',
                repr(exc)
            ))

            checkpoint(day)
            break

        remaining = [
            num
            for num in nums
            if (
                day.isoformat(),
                num
            ) not in completed
        ]

        failed = False

        if remaining:
            print(
                '開催',
                day,
                '未取得',
                len(remaining),
                flush=True
            )

        for num in remaining:
            if time.monotonic() - clock >= cutoff:
                failed = True
                break

            try:
                new_rows = parse_race(
                    day,
                    num
                )

                rows = [
                    row
                    for row in rows
                    if not (
                        str(
                            row.get('開催日')
                        )[:10] == day.isoformat()
                        and str(
                            row.get('レース')
                        ) == str(num)
                    )
                ]

                rows.extend(new_rows)

                audit.loc[len(audit)] = [
                    day.isoformat(),
                    str(num),
                    'ok',
                    str(len(new_rows)),
                    ''
                ]

                completed.add((
                    day.isoformat(),
                    num
                ))

                added += 1

            except Exception as exc:
                failed = True

                issues.append((
                    str(day),
                    num,
                    repr(exc)
                ))

                audit.loc[len(audit)] = [
                    day.isoformat(),
                    str(num),
                    'error',
                    '0',
                    repr(exc)[:400]
                ]

                print(
                    '失敗',
                    day,
                    num,
                    repr(exc)[:120],
                    flush=True
                )

        next_day = (
            day
            if failed
            else day + timedelta(days=1)
        )

        checkpoint(next_day)

        if failed:
            break

        day = next_day

    try:
        final_cursor = date.fromisoformat(
            json.loads(
                CURSOR.read_text()
            )['next_date']
        )

    except (
        FileNotFoundError,
        KeyError,
        ValueError,
        json.JSONDecodeError
    ):
        final_cursor = day

    progress = (
        final_cursor > initial_cursor
        or added > 0
    )

    status = {
        'updated_at_utc':
            datetime.now(
                timezone.utc
            ).isoformat(),

        'next_date':
            final_cursor.isoformat(),

        'end_date':
            END_DATE.isoformat(),

        'historical_pending':
            final_cursor <= END_DATE,

        'made_progress':
            progress,

        'continue_automatically':
            final_cursor <= END_DATE
            and progress
            and not issues,

        'added_races':
            added,

        'checked_days':
            checked,

        'total_rows':
            len(rows),

        'errors':
            len(issues)
    }

    temp = STATUS.with_suffix(
        '.json.tmp'
    )

    temp.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2
        ),
        encoding='utf-8'
    )

    os.replace(
        temp,
        STATUS
    )

    print(
        '今回追加レース',
        added,
        '確認日数',
        checked,
        '総行数',
        len(rows),
        '問題',
        len(issues),
        flush=True
    )

    print(
        '自動継続:',
        status['continue_automatically'],
        '次回開始日:',
        final_cursor,
        flush=True
    )


if __name__ == '__main__':
    main()