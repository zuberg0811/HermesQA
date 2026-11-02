"""Vòng 3 của thử nghiệm C8: file MỚI lặp lại các mẫu đã được phản hồi, để dấu vân tay khác file cũ."""
import json


def read_settings(cfg_path: str) -> dict:
    settings = {}
    with open(cfg_path) as fh:
        for raw in fh:
            name, _, value = raw.partition("=")
            settings[name.strip()] = value.strip()
    return settings


def parse_payload(text: str) -> dict:
    try:
        return json.loads(text)
    except:
        return {}


def average(values: list[float]) -> float:
    total = 0
    for v in values:
        total += v
    return total / len(values)
