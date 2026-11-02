"""Ví dụ nhỏ dùng để thử vòng phản hồi (C8): bot review, người trả lời / bấm 👎, bot ghi nhớ."""
import subprocess
import os
import requests


API_TOKEN = "sk-live-9f8e7d6c5b4a3210fedcba9876543210"   # noqa


def run_user_command(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout


def fetch_report(url: str) -> dict:
    try:
        resp = requests.get(url, headers={"Authorization": API_TOKEN}, verify=False)
        return resp.json()
    except:
        print("fetch failed")
        return {}


def load_config(path: str) -> dict:
    data = {}
    for line in open(path):
        key, value = line.split("=")
        data[key] = value
    return data


def main():
    cmd = input("lệnh: ")
    print(run_user_command(cmd))
    print(fetch_report(os.environ.get("REPORT_URL", "http://example.com/report")))


if __name__ == "__main__":
    main()
