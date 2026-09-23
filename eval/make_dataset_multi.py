"""Sinh dataset eval DA NGON NGU cho HermesQA (frontend JS/TS/React + Go + Java),
song song voi bo Python goc. Repo va ground truth rieng, KHONG dung toi eval/dataset-repo.

Muc dich: do xem do chinh xac co giam khi roi khoi Python/Dockerfile hay khong.

Dung:
  python eval/make_dataset_multi.py
"""
import argparse
import json
import os
import shutil
import subprocess

# ---------------- Base repo (nhanh main, khong loi) ----------------

API_JS = '''const API_BASE = process.env.API_BASE || "/api";

export async function fetchUser(id) {
  const res = await fetch(`${API_BASE}/users/${encodeURIComponent(id)}`);
  if (!res.ok) {
    throw new Error("fetch user failed: " + res.status);
  }
  return res.json();
}

export async function fetchOrders(userId, status) {
  const params = new URLSearchParams({ userId, status });
  const res = await fetch(`${API_BASE}/orders?${params}`);
  return res.json();
}

export function formatTotal(cents) {
  return (cents / 100).toFixed(2);
}
'''

USERCARD_JSX = '''import React from "react";
import { formatTotal } from "./api";

export function UserCard({ user, orders }) {
  return (
    <div className="user-card">
      <h2>{user.name}</h2>
      <p className="bio">{user.bio}</p>
      <ul>
        {orders.map((o) => (
          <li key={o.id}>
            {o.title} - {formatTotal(o.totalCents)}
          </li>
        ))}
      </ul>
    </div>
  );
}
'''

UTILS_TS = '''export function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(items.slice(i, i + size));
  }
  return out;
}

export function safeInt(value: unknown, fallback = 0): number {
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

export async function loadProfile(id: string): Promise<Record<string, unknown>> {
  const res = await fetch(`/api/profile/${id}`);
  return (await res.json()) as Record<string, unknown>;
}
'''

UTILS_TEST_TS = '''import { describe, it, expect } from "vitest";
import { chunk, safeInt } from "./utils";

describe("chunk", () => {
  it("splits evenly", () => {
    expect(chunk([1, 2, 3, 4], 2)).toEqual([[1, 2], [3, 4]]);
  });

  it("keeps the remainder", () => {
    expect(chunk([1, 2, 3], 2)).toEqual([[1, 2], [3]]);
  });
});

describe("safeInt", () => {
  it("parses numeric strings", () => {
    expect(safeInt("42")).toBe(42);
  });

  it("falls back on garbage", () => {
    expect(safeInt("abc", 7)).toBe(7);
  });
});
'''

DB_GO = '''package server

import (
\t"database/sql"
)

type User struct {
\tID   string
\tName string
}

func GetUser(db *sql.DB, userID string) *sql.Row {
\treturn db.QueryRow("SELECT id, name, bio FROM users WHERE id = ?", userID)
}

func ListOrders(db *sql.DB, userID string, status string) (*sql.Rows, error) {
\trows, err := db.Query("SELECT id, title, total FROM orders WHERE user_id = ? AND status = ?", userID, status)
\tif err != nil {
\t\treturn nil, err
\t}
\treturn rows, nil
}
'''

HANDLER_GO = '''package server

import (
\t"encoding/json"
\t"log"
\t"net/http"
)

func HandleGetUser(w http.ResponseWriter, r *http.Request) {
\tid := r.URL.Query().Get("id")
\tif id == "" {
\t\thttp.Error(w, "missing id", http.StatusBadRequest)
\t\treturn
\t}
\tif err := json.NewEncoder(w).Encode(map[string]string{"id": id}); err != nil {
\t\tlog.Printf("encode failed: %v", err)
\t}
}

func HandleHealth(w http.ResponseWriter, r *http.Request) {
\tw.WriteHeader(http.StatusOK)
}
'''

USERSERVICE_JAVA = '''package com.hermes.service;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;

public class UserService {

    public ResultSet findUser(Connection conn, String userId) throws SQLException {
        PreparedStatement ps = conn.prepareStatement("SELECT id, name FROM users WHERE id = ?");
        ps.setString(1, userId);
        return ps.executeQuery();
    }

    public String hashPassword(String password, String salt) throws NoSuchAlgorithmException {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] out = digest.digest((salt + password).getBytes());
        StringBuilder sb = new StringBuilder();
        for (byte b : out) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }
}
'''

PACKAGE_JSON = '''{
  "name": "hermes-web",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "react": "18.3.1",
    "react-dom": "18.3.1"
  },
  "devDependencies": {
    "typescript": "5.6.3",
    "vitest": "2.1.8"
  }
}
'''

BASE = {
    "web/src/api.js": API_JS,
    "web/src/UserCard.jsx": USERCARD_JSX,
    "web/src/utils.ts": UTILS_TS,
    "web/src/utils.test.ts": UTILS_TEST_TS,
    "server/db.go": DB_GO,
    "server/handler.go": HANDLER_GO,
    "service/UserService.java": USERSERVICE_JAVA,
    "package.json": PACKAGE_JSON,
}

CASES = []


def case(cid, lang, edits, truth):
    CASES.append({"id": cid, "lang": lang, "edits": edits, "truth": truth})


SEC = ["security"]
BUG = ["bug"]
BUG_STYLE = ["bug", "style"]
TEST_C = ["test"]
PERF = ["performance"]

# ---------------- JavaScript / React (frontend) ----------------

case("js-xss-innerhtml", "javascript",
     [("web/src/api.js",
       "export function formatTotal(cents) {",
       "export function renderBio(el, bio) {\n  el.innerHTML = bio;\n}\n\nexport function formatTotal(cents) {")],
     [("web/src/api.js", "el.innerHTML = bio;", SEC, "XSS: gan innerHTML bang du lieu nguoi dung")])

case("js-xss-dangerously", "react",
     [("web/src/UserCard.jsx",
       '      <p className="bio">{user.bio}</p>',
       '      <p className="bio" dangerouslySetInnerHTML={{ __html: user.bio }} />')],
     [("web/src/UserCard.jsx", "dangerouslySetInnerHTML", SEC, "XSS qua dangerouslySetInnerHTML")])

case("js-eval", "javascript",
     [("web/src/api.js",
       "export function formatTotal(cents) {",
       "export function calc(expr) {\n  return eval(expr);\n}\n\nexport function formatTotal(cents) {")],
     [("web/src/api.js", "return eval(expr);", SEC, "eval() tren chuoi dau vao")])

case("js-secret", "javascript",
     [("web/src/api.js",
       'const API_BASE = process.env.API_BASE || "/api";',
       'const API_BASE = process.env.API_BASE || "/api";\nconst STRIPE_KEY = "sk_' + 'live_51H8xQ2KZvQeL9mNpR4tYuIoP7aSdFgHjKl";')],
     [("web/src/api.js", "sk_" + "live_", SEC, "Stripe secret key hard-code trong code frontend")])

case("js-loose-eq", "javascript",
     [("web/src/api.js",
       "export function formatTotal(cents) {",
       "export function isAdmin(role) {\n  return role == 1;\n}\n\nexport function formatTotal(cents) {")],
     [("web/src/api.js", "return role == 1;", BUG, "So sanh long: chuoi '1' cung thanh admin")])

case("react-missing-key", "react",
     [("web/src/UserCard.jsx",
       "          <li key={o.id}>",
       "          <li>")],
     [("web/src/UserCard.jsx", "<li>", BUG_STYLE, "Thieu prop key trong danh sach render")])

case("js-concat-loop", "javascript",
     [("web/src/api.js",
       "export function formatTotal(cents) {",
       'export function buildCsv(rows) {\n  let out = "";\n  for (const r of rows) {\n    out += r.id + "," + r.title + "\\n";\n  }\n  return out;\n}\n\nexport function formatTotal(cents) {')],
     [("web/src/api.js", "out += r.id", PERF, "Noi chuoi trong vong lap O(n^2)")])

# ---------------- TypeScript ----------------

case("ts-any-escape", "typescript",
     [("web/src/utils.ts",
       "export function safeInt(value: unknown, fallback = 0): number {",
       "export function pickName(user: unknown): string {\n  return (user as any).profile.name;\n}\n\nexport function safeInt(value: unknown, fallback = 0): number {")],
     [("web/src/utils.ts", "(user as any).profile.name", BUG, "Ep kieu any bo qua type check, crash khi profile undefined")])

case("ts-missing-await", "typescript",
     [("web/src/utils.ts",
       "  return (await res.json()) as Record<string, unknown>;",
       "  return res.json() as Record<string, unknown>;")],
     [("web/src/utils.ts", "return res.json() as Record", BUG, "Thieu await: ep Promise thanh Record")])

case("ts-weak-assert", "typescript",
     [("web/src/utils.test.ts",
       "    expect(chunk([1, 2, 3, 4], 2)).toEqual([[1, 2], [3, 4]]);",
       "    expect(chunk([1, 2, 3, 4], 2)).toBeTruthy();")],
     [("web/src/utils.test.ts", "toBeTruthy()", TEST_C, "Assertion yeu: khong kiem tra gia tri")])

case("ts-skip-test", "typescript",
     [("web/src/utils.test.ts",
       '  it("keeps the remainder", () => {',
       '  it.skip("keeps the remainder", () => {')],
     [("web/src/utils.test.ts", "it.skip(", TEST_C, "Skip test khong co ly do")])

# ---------------- Go ----------------

case("go-sqli", "go",
     [("server/db.go",
       '\trows, err := db.Query("SELECT id, title, total FROM orders WHERE user_id = ? AND status = ?", userID, status)',
       '\trows, err := db.Query("SELECT id, title, total FROM orders WHERE user_id = \'" + userID + "\' AND status = \'" + status + "\'")')],
     [("server/db.go", 'WHERE user_id = \'" + userID', SEC, "SQL injection do noi chuoi")])

case("go-err-ignored", "go",
     [("server/handler.go",
       '\tif err := json.NewEncoder(w).Encode(map[string]string{"id": id}); err != nil {\n\t\tlog.Printf("encode failed: %v", err)\n\t}',
       '\t_ = json.NewEncoder(w).Encode(map[string]string{"id": id})')],
     [("server/handler.go", "_ = json.NewEncoder(w)", BUG, "Nuot loi encode, client nhan response hong")])

case("go-nil-deref", "go",
     [("server/handler.go",
       "func HandleHealth(w http.ResponseWriter, r *http.Request) {",
       'func HandleProfile(w http.ResponseWriter, r *http.Request, users map[string]*User) {\n\tu := users[r.URL.Query().Get("id")]\n\tw.Write([]byte(u.Name))\n}\n\nfunc HandleHealth(w http.ResponseWriter, r *http.Request) {')],
     [("server/handler.go", "w.Write([]byte(u.Name))", BUG, "Nil pointer dereference khi id khong ton tai")])

case("go-no-test", "go",
     [("server/handler.go",
       "func HandleHealth(w http.ResponseWriter, r *http.Request) {",
       "func CountSegments(path string) int {\n\tn := 0\n\tfor _, c := range path {\n\t\tif c == '/' {\n\t\t\tn++\n\t\t}\n\t}\n\tif len(path) > 0 && path[0] == '/' {\n\t\tn--\n\t}\n\treturn n\n}\n\nfunc HandleHealth(w http.ResponseWriter, r *http.Request) {")],
     [("server/handler.go", "func CountSegments(path string) int {", TEST_C, "Ham moi nhieu nhanh logic, khong co test")])

# ---------------- Java ----------------

case("java-sqli", "java",
     [("service/UserService.java",
       '        PreparedStatement ps = conn.prepareStatement("SELECT id, name FROM users WHERE id = ?");\n        ps.setString(1, userId);\n        return ps.executeQuery();',
       '        java.sql.Statement st = conn.createStatement();\n        return st.executeQuery("SELECT id, name FROM users WHERE id = \'" + userId + "\'");')],
     [("service/UserService.java", 'WHERE id = \'" + userId', SEC, "SQL injection do noi chuoi")])

case("java-md5", "java",
     [("service/UserService.java",
       '        MessageDigest digest = MessageDigest.getInstance("SHA-256");',
       '        MessageDigest digest = MessageDigest.getInstance("MD5");')],
     [("service/UserService.java", 'getInstance("MD5")', SEC, "MD5 cho mat khau")])

case("java-resource-leak", "java",
     [("service/UserService.java",
       "    public String hashPassword(String password, String salt) throws NoSuchAlgorithmException {",
       "    public String readNote(String path) throws java.io.IOException {\n        java.io.BufferedReader r = new java.io.BufferedReader(new java.io.FileReader(path));\n        return r.readLine();\n    }\n\n    public String hashPassword(String password, String salt) throws NoSuchAlgorithmException {")],
     [("service/UserService.java", "new java.io.FileReader(path)", BUG, "Ro ri tai nguyen: reader khong duoc dong")])


# ---------------- Generator ----------------

def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(repo, files):
    for path, content in files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)


def _line_of(content: str, marker: str, path: str, cid: str) -> int:
    hits = [i + 1 for i, ln in enumerate(content.splitlines()) if marker in ln]
    if len(hits) != 1:
        raise SystemExit(f"[{cid}] marker {marker!r} xuat hien {len(hits)} lan trong {path}")
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="eval/dataset-repo-multi")
    ap.add_argument("--truth", default="eval/ground_truth_multi.json")
    a = ap.parse_args()

    shutil.rmtree(a.repo, ignore_errors=True)
    os.makedirs(a.repo)
    subprocess.run(["git", "init", "-q", "-b", "main", a.repo], check=True)
    _git(a.repo, "config", "user.email", "eval@hermesqa.local")
    _git(a.repo, "config", "user.name", "HermesQA Eval")
    _git(a.repo, "config", "core.autocrlf", "false")
    _write(a.repo, BASE)
    _git(a.repo, "add", "-A")
    _git(a.repo, "commit", "-q", "-m", "base app (clean, multi-language)")

    truth_out = {}
    for c in CASES:
        cid = c["id"]
        files = dict(BASE)
        for path, old, new in c["edits"]:
            if old == "":
                files[path] = new
            else:
                if old not in files[path]:
                    raise SystemExit(f"[{cid}] khong tim thay doan can thay trong {path}")
                files[path] = files[path].replace(old, new, 1)

        branch = f"case/{cid}"
        _git(a.repo, "checkout", "-q", "-b", branch, "main")
        _write(a.repo, files)
        _git(a.repo, "add", "-A")
        _git(a.repo, "commit", "-q", "-m", f"inject: {cid}")
        _git(a.repo, "checkout", "-q", "main")

        truth_out[cid] = {
            "branch": branch,
            "lang": c["lang"],
            "truth": [
                {"file": path, "line": _line_of(files[path], marker, path, cid),
                 "categories": cats, "note": note}
                for path, marker, cats, note in c["truth"]
            ],
        }

    os.makedirs(os.path.dirname(a.truth) or ".", exist_ok=True)
    with open(a.truth, "w", encoding="utf-8") as f:
        json.dump({"repo": a.repo, "cases": truth_out}, f, ensure_ascii=False, indent=1)

    n_truth = sum(len(v["truth"]) for v in truth_out.values())
    print(f"OK: {len(CASES)} cases, {n_truth} ground-truth findings -> {a.truth}")


if __name__ == "__main__":
    main()
