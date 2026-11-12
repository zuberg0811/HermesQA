"""Vòng 4 của thử nghiệm C8: file thứ ba lặp lại hai mẫu đã bị 👎 hai lần, kỳ vọng bot không đăng nữa."""
import csv


def read_rows(path: str) -> list[dict]:
    rows = []
    with open(path) as src:
        for row in csv.DictReader(src):
            rows.append(row)
    return rows


def safe_int(text: str) -> int:
    try:
        return int(text)
    except:
        return 0
