"""Phát hiện thứ HẾT HẠN THEO THỜI GIAN và code LỖI THỜI / KHÔNG TỐI ƯU. Chạy thuần Python, không cần Docker hay LLM,
nên có mặt trong mọi đường chạy (worker, cli, eval, trang web demo với --skip-static).

Hai nhóm, hai phạm vi khác nhau (xem `scan`):
  1. KHOÁ / CHỨNG CHỈ HẾT HẠN — quét CẢ FILE thay đổi, kể cả dòng diff không đụng tới: token JWT hard-code có `exp`
     đã qua, chứng chỉ PEM đã quá notAfter, dòng ghi hạn dùng (expires / valid_until / not_after...) kèm từ khoá key/
     token/secret mà ngày đã qua. Một khoá hết hạn là sự thật tại THỜI ĐIỂM review, không phụ thuộc ai đụng vào dòng đó.
     Bỏ qua file test: fixture hết hạn thường là cố ý.
  2. CODE LỖI THỜI / KHÔNG TỐI ƯU — chỉ các dòng `+` của diff (hoặc cả file khi STALE_SCOPE=file), theo đúng
     nguyên tắc "code cũ không bị diff đụng tới thì không báo" của checklist: runtime / image / runner hết hạn hỗ trợ
     (so với bảng ngày EOL), action CI và lệnh workflow đã bị GitHub bỏ, thư viện bị bỏ rơi trong manifest, API đã
     deprecated hoặc bị xoá theo ngôn ngữ (Python/JS/Go/Java/Dockerfile), và vài mẫu không tối ưu có thể nhận ra
     chắc chắn bằng mẫu (nối chuỗi += trong vòng lặp, time.sleep trong coroutine, sorted(...)[0]...).

Nguyên tắc như static_tools: chỉ báo thứ KIỂM ĐƯỢC bằng máy; phần cần hiểu ngữ cảnh để cho LLM (checklist trong
app/skills/rules/ có mục "Lỗi thời" tương ứng). Bảng EOL ghi NGÀY chứ không ghi "đã hết hạn", để kết quả tự đúng
theo thời gian mà không phải sửa code. Mọi finding mang source="stale", role="static:stale", title "<MÃ>: ..." để
calibrate / feedback đếm theo mã luật như các tool khác.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from app.config import settings
from app.review.diff_utils import ChangedFile
from app.review.schemas import Finding, SEVERITY_RANK
from app.review.selection import is_generated_path
from app.review.static_tools import is_test_path

log = logging.getLogger("hermesqa.stale")

SOURCE = "stale"
ROLE = "static:stale"
MAX_FILE_BYTES = 2_000_000
MAX_PER_FILE = 50
EOL_WARN_DAYS = 90          # runtime sắp hết hạn hỗ trợ: báo trước 90 ngày (mức low)

# ---------------------------------------------------------------- bảng ngày (EOL = ngày ngừng bản vá bảo mật)

def _d(s: str) -> date:
    return date.fromisoformat(s)


PYTHON_EOL = {"2.7": "2020-01-01", "3.5": "2020-09-13", "3.6": "2021-12-23", "3.7": "2023-06-27", "3.8": "2024-10-07",
              "3.9": "2025-10-31", "3.10": "2026-10-31", "3.11": "2027-10-31", "3.12": "2028-10-31",
              "3.13": "2029-10-31", "3.14": "2030-10-31"}
NODE_EOL = {"8": "2019-12-31", "10": "2021-04-30", "12": "2022-04-30", "14": "2023-04-30", "16": "2023-09-11",
            "18": "2025-04-30", "20": "2026-04-30", "22": "2027-04-30", "24": "2028-04-30"}
UBUNTU_EOL = {"14.04": "2019-04-30", "16.04": "2021-04-30", "18.04": "2023-05-31", "20.04": "2025-05-31",
              "22.04": "2027-06-01", "24.04": "2029-05-31"}
UBUNTU_NAMES = {"trusty": "14.04", "xenial": "16.04", "bionic": "18.04", "focal": "20.04", "jammy": "22.04", "noble": "24.04"}
DEBIAN_EOL = {"8": "2020-06-30", "9": "2022-06-30", "10": "2024-06-30", "11": "2026-08-31", "12": "2028-06-30"}
DEBIAN_NAMES = {"jessie": "8", "stretch": "9", "buster": "10", "bullseye": "11", "bookworm": "12"}
CENTOS_EOL = {"6": "2020-11-30", "7": "2024-06-30", "8": "2021-12-31"}
ALPINE_EOL = {"3.14": "2023-05-01", "3.15": "2023-11-01", "3.16": "2024-05-23", "3.17": "2024-11-22", "3.18": "2025-05-09",
              "3.19": "2025-11-01", "3.20": "2026-04-01", "3.21": "2026-11-01", "3.22": "2027-05-01"}
GO_EOL = {"1.18": "2023-02-01", "1.19": "2023-08-08", "1.20": "2024-02-06", "1.21": "2024-08-13", "1.22": "2025-02-11",
          "1.23": "2025-08-12", "1.24": "2026-02-10", "1.25": "2026-08-11"}
RUBY_EOL = {"2.6": "2022-04-12", "2.7": "2023-03-31", "3.0": "2024-04-23", "3.1": "2025-03-26", "3.2": "2026-03-31", "3.3": "2027-03-31"}
PHP_EOL = {"7.3": "2021-12-06", "7.4": "2022-11-28", "8.0": "2023-11-26", "8.1": "2025-12-31", "8.2": "2026-12-31", "8.3": "2027-12-31"}
POSTGRES_EOL = {"9.6": "2021-11-11", "10": "2022-11-10", "11": "2023-11-09", "12": "2024-11-14", "13": "2025-11-13",
                "14": "2026-11-12", "15": "2027-11-11", "16": "2028-11-09", "17": "2029-11-08"}
MYSQL_EOL = {"5.6": "2021-02-05", "5.7": "2023-10-31", "8.0": "2026-04-30"}
MONGO_EOL = {"4.2": "2023-04-30", "4.4": "2024-02-29", "5.0": "2024-10-31", "6.0": "2025-07-31"}
DJANGO_EOL = {"1.11": "2020-04-01", "2.0": "2019-04-01", "2.1": "2019-12-02", "2.2": "2022-04-11", "3.0": "2021-04-06",
              "3.1": "2021-12-07", "3.2": "2024-04-01", "4.0": "2023-04-01", "4.1": "2023-12-01", "4.2": "2026-04-01",
              "5.0": "2025-04-02", "5.1": "2025-12-02", "5.2": "2028-04-01"}
# Runner GitHub-hosted bị gỡ: job dùng nhãn này không bao giờ được nhận.
RUNNER_REMOVED = {"ubuntu-16.04": "2021-09-20", "ubuntu-18.04": "2022-08-08", "ubuntu-20.04": "2025-04-15",
                  "windows-2016": "2022-06-30", "windows-2019": "2025-06-30",
                  "macos-10.15": "2022-12-01", "macos-11": "2024-06-28", "macos-12": "2024-12-03", "macos-13": "2025-12-04"}

# tên image (đã bỏ registry) -> (tên hiển thị, bảng, số thành phần version lấy từ tag, bảng codename)
IMAGE_TABLES: dict[str, tuple[str, dict, int, dict]] = {
    "python": ("Python", PYTHON_EOL, 2, {}), "node": ("Node.js", NODE_EOL, 1, {}),
    "ubuntu": ("Ubuntu", UBUNTU_EOL, 2, UBUNTU_NAMES), "debian": ("Debian", DEBIAN_EOL, 1, DEBIAN_NAMES),
    "centos": ("CentOS", CENTOS_EOL, 1, {}), "alpine": ("Alpine", ALPINE_EOL, 2, {}),
    "golang": ("Go", GO_EOL, 2, {}), "ruby": ("Ruby", RUBY_EOL, 2, {}), "php": ("PHP", PHP_EOL, 2, {}),
    "postgres": ("PostgreSQL", POSTGRES_EOL, 1, {}), "mysql": ("MySQL", MYSQL_EOL, 2, {}), "mongo": ("MongoDB", MONGO_EOL, 2, {}),
}


def _version_key(ver: str, parts: int) -> str:
    """'3.9.18' với parts=2 -> '3.9'; '9.6' với parts=1 -> '9.6' (Postgres 9.x đếm theo 2 thành phần)."""
    nums = ver.split(".")
    if parts == 1 and nums[0] == "9" and len(nums) > 1:
        return ".".join(nums[:2])
    return ".".join(nums[:parts])


def eol_status(table: dict[str, str], key: str, today: date) -> tuple[str | None, date | None]:
    """('expired' | 'soon' | None, ngày EOL). Phiên bản không có trong bảng -> (None, None): không đoán."""
    s = table.get(key)
    if not s:
        return None, None
    eol = _d(s)
    if eol <= today:
        return "expired", eol
    if (eol - today).days <= EOL_WARN_DAYS:
        return "soon", eol
    return None, eol


def latest_supported(table: dict[str, str], today: date) -> str:
    ok = [k for k, s in table.items() if _d(s) > today]
    return max(ok, key=lambda k: tuple(int(x) for x in k.split("."))) if ok else ""


# ---------------------------------------------------------------- luật theo mẫu (một dòng)

@dataclass(frozen=True)
class Rule:
    id: str
    pattern: str
    title: str                 # có thể dùng {0} = nhóm bắt đầu tiên không rỗng
    explanation: str
    fix: str = ""
    severity: str = "medium"
    category: str = "maintainability"
    confidence: float = 0.85
    ctx: str = ""              # "" | "async" (trong async def) | "loop" (trong vòng lặp) — chỉ Python

    def rx(self) -> re.Pattern:
        return _compiled(self.pattern)


_RX_CACHE: dict[str, re.Pattern] = {}


def _compiled(p: str) -> re.Pattern:
    r = _RX_CACHE.get(p)
    if r is None:
        r = _RX_CACHE[p] = re.compile(p)
    return r


PY2_FIX = "Chuyển sang cú pháp Python 3 (2to3 / pyupgrade)."
PEP594 = ("imp", "distutils", "asyncore", "asynchat", "smtpd", "cgi", "cgitb", "telnetlib", "nntplib", "pipes", "crypt",
          "imghdr", "sndhdr", "audioop", "chunk", "mailcap", "msilib", "nis", "ossaudiodev", "spwd", "sunau", "uu",
          "xdrlib", "lib2to3")

PYTHON_RULES = [
    Rule("PY-UTCNOW", r"\b(?:datetime\.)?utcnow\(\)|\.utcfromtimestamp\(", "datetime.utcnow() đã deprecated (Python 3.12)",
         "utcnow()/utcfromtimestamp() trả về datetime naive (không có tzinfo) nên so sánh hay trừ với datetime aware sẽ ném TypeError, "
         "và Python 3.12 phát cảnh báo deprecated, sẽ bị xoá ở bản sau.",
         "datetime.now(timezone.utc)  /  datetime.fromtimestamp(ts, tz=timezone.utc)", "medium", "maintainability", 0.9),
    Rule("PY-REMOVED-MODULE", r"^\s*(?:from|import)\s+(" + "|".join(PEP594) + r")\b",
         "Module `{0}` đã bị xoá khỏi Python 3.12/3.13 (PEP 594)",
         "Import này ném ModuleNotFoundError trên Python 3.12+ (imp, distutils, asyncore...) hoặc 3.13+ (cgi, telnetlib, pipes...).",
         "Dùng thư viện thay thế: imp -> importlib, distutils -> setuptools/sysconfig, cgi -> email.message / urllib.parse, "
         "pipes -> shlex, telnetlib -> telnetlib3, lib2to3 -> pyupgrade/ast.", "medium", "bug", 0.85),
    Rule("PY-COLLECTIONS-ABC", r"\bcollections\.(Mapping|MutableMapping|Sequence|MutableSequence|Iterable|Iterator|Callable|Set|MutableSet|Hashable|Sized|Container)\b",
         "collections.{0} đã bị xoá từ Python 3.10", "Các ABC này chỉ còn ở collections.abc; truy cập qua collections ném AttributeError trên mọi Python còn được hỗ trợ.",
         "from collections.abc import {0}", "high", "bug", 0.95),
    Rule("PY-GETARGSPEC", r"\binspect\.getargspec\(", "inspect.getargspec() đã bị xoá từ Python 3.11",
         "Ném AttributeError trên Python 3.11+.", "inspect.signature(fn) hoặc inspect.getfullargspec(fn)", "medium", "bug", 0.9),
    Rule("PY-TIME-CLOCK", r"\btime\.clock\(\)", "time.clock() đã bị xoá từ Python 3.8", "Ném AttributeError.",
         "time.perf_counter() (đo thời gian) hoặc time.process_time() (CPU)", "high", "bug", 0.95),
    Rule("PY-THREAD-ISALIVE", r"\.isAlive\(\)", "Thread.isAlive() đã bị xoá từ Python 3.9", "Ném AttributeError.", ".is_alive()", "medium", "bug", 0.85),
    Rule("PY-SETDAEMON", r"\.setDaemon\(", "Thread.setDaemon() đã deprecated (3.10)", "Dùng thuộc tính daemon.",
         "t.daemon = True (hoặc Thread(..., daemon=True))", "medium", "maintainability", 0.8),
    Rule("PY-LOGGER-WARN", r"\b(?:logging|log|logger|_log|_logger|LOG|LOGGER)\.warn\(", "Logger.warn() đã bị xoá từ Python 3.13",
         "warn() chỉ là alias cũ của warning(); Python 3.13 xoá hẳn -> AttributeError.", ".warning(", "medium", "bug", 0.85),
    Rule("PY-UNITTEST-ALIAS", r"\.(assertEquals|assertNotEquals|assertRegexpMatches|assertNotRegexpMatches|assert_|failUnless|failIf|assertDictContainsSubset)\(",
         "unittest.{0}() đã bị xoá từ Python 3.12", "Các alias cũ của unittest bị xoá -> test ném AttributeError trên 3.12+.",
         "assertEqual / assertNotEqual / assertRegex / assertTrue", "medium", "bug", 0.9),
    Rule("PY-PKG-RESOURCES", r"^\s*(?:from|import)\s+pkg_resources\b", "pkg_resources đã deprecated (setuptools)",
         "pkg_resources chậm khi import (quét mọi distribution) và setuptools đã tuyên bố ngừng hỗ trợ; nhiều môi trường mới không cài sẵn setuptools.",
         "importlib.metadata (version, entry_points) / importlib.resources (file dữ liệu)", "medium", "maintainability", 0.8),
    Rule("PY-PLATFORM-DIST", r"\bplatform\.(linux_distribution|dist)\(", "platform.{0}() đã bị xoá từ Python 3.8", "Ném AttributeError.",
         "Đọc /etc/os-release hoặc dùng gói `distro`.", "high", "bug", 0.95),
    Rule("PY-SSL-WRAP", r"\bssl\.wrap_socket\(", "ssl.wrap_socket() đã bị xoá từ Python 3.12", "Ném AttributeError, và hàm này không kiểm tra hostname.",
         "ctx = ssl.create_default_context(); ctx.wrap_socket(sock, server_hostname=host)", "medium", "bug", 0.9),
    Rule("PY-OLD-TLS", r"\bssl\.PROTOCOL_(SSLv2|SSLv3|TLSv1|TLSv1_1)\b", "Giao thức ssl.PROTOCOL_{0} đã lỗi thời và không an toàn",
         "SSLv3/TLS 1.0/1.1 đã bị khai tử (RFC 8996); hằng này deprecated từ 3.10 và nhiều OpenSSL mới từ chối.",
         "ssl.create_default_context() hoặc PROTOCOL_TLS_CLIENT với minimum_version = TLSVersion.TLSv1_2", "medium", "security", 0.9),
    Rule("PY-NUMPY-ALIAS", r"\bnp\.(float|int|bool|object|str|complex|long|unicode)\b(?![\w.])", "np.{0} đã bị xoá từ NumPy 1.24",
         "Alias np.float/np.int... chỉ là kiểu Python thường và đã bị xoá -> AttributeError.", "Dùng float / np.float64 / np.int64 / bool tuỳ ý định.",
         "high", "bug", 0.9),
    Rule("PY2-PRINT", r"^\s*print\s+(?:\"|'|\w+\s*$|\w+\s*%)", "Câu lệnh print kiểu Python 2", "SyntaxError trên Python 3.", PY2_FIX, "high", "bug", 0.85),
    # `long` / `unicode` chỉ bắt ở dạng gọi hàm: dạng tên trần trùng với biến thường (long, lat = ...) và chữ Việt không dấu.
    Rule("PY2-NAME", r"\b(xrange|raw_input|unichr|basestring|execfile|unicode)\(|\b(xrange|basestring)\b(?=\s*[,)\]:])",
         "Tên Python 2 không còn tồn tại trên Python 3", "NameError khi chạy tới dòng này.", PY2_FIX, "high", "bug", 0.85),
    Rule("PY2-DICT-ITER", r"\.(iteritems|itervalues|iterkeys|has_key)\(", "dict.{0}() không tồn tại trên Python 3",
         "AttributeError khi chạy (pandas cũng đã bỏ iteritems từ 2.0).", ".items() / .values() / .keys() / `in`", "high", "bug", 0.85),
    Rule("PY2-IMPORT", r"^\s*(?:from|import)\s+(urllib2|urlparse|cPickle|cStringIO|ConfigParser|Queue|SocketServer|httplib|HTMLParser|Tkinter|__builtin__)\b",
         "Module Python 2 `{0}` không tồn tại trên Python 3", "ModuleNotFoundError.", "urllib.request / urllib.parse / pickle / io / configparser / queue / socketserver / http.client / html.parser / tkinter / builtins",
         "high", "bug", 0.9),
    Rule("PY2-EXCEPT", r"^\s*except\s+\w+(?:\.\w+)*\s*,\s*\w+\s*:", "Cú pháp `except X, e` của Python 2", "SyntaxError trên Python 3.", "except X as e:", "high", "bug", 0.95),
    Rule("PY-SETUP-INSTALL", r"\bsetup\.py\s+(install|test|develop|sdist|bdist_wheel)\b", "`setup.py {0}` đã deprecated (setuptools)",
         "setuptools in cảnh báo và sẽ bỏ các lệnh này; `setup.py test` đã bị xoá từ setuptools 72.",
         "pip install . / pip install -e . / python -m build / pytest", "medium", "devops", 0.85),
    # không tối ưu, nhận ra chắc chắn bằng mẫu
    Rule("PY-SORTED-FIRST", r"\bsorted\([^()]*(?:\([^()]*\)[^()]*)*\)\s*\[\s*(0|-1)\s*\]", "sorted(...)[{0}] sắp xếp cả dãy chỉ để lấy một phần tử",
         "O(n log n) và cấp phát list mới, trong khi min()/max() chỉ cần một lượt O(n).", "min(xs, key=...) / max(xs, key=...)", "medium", "performance", 0.8),
    Rule("PY-STR-CONCAT-LOOP", r"^\s*\w+(?:\[[^\]]*\])?\s*\+=\s*(?:f?[\"']|str\()", "Nối chuỗi bằng += trong vòng lặp",
         "Mỗi lần += tạo chuỗi mới và chép lại toàn bộ -> O(n²) khi dãy dài.", "Gom các mảnh vào list rồi ''.join(parts), hoặc io.StringIO.",
         "medium", "performance", 0.7, ctx="loop"),
    Rule("PY-RECOMPILE-LOOP", r"\bre\.compile\(", "re.compile() bên trong vòng lặp", "Biên dịch lại cùng một regex ở mỗi vòng; cache của re có giới hạn và vẫn tốn tra cứu.",
         "Biên dịch một lần ngoài vòng lặp (hằng ở mức module).", "medium", "performance", 0.75, ctx="loop"),
    Rule("PY-CONCAT-LOOP", r"\bpd\.concat\(|\bpandas\.concat\(", "pd.concat() bên trong vòng lặp", "Mỗi lần concat chép lại toàn bộ DataFrame -> O(n²).",
         "Gom các khung vào list rồi pd.concat(list) một lần sau vòng lặp.", "medium", "performance", 0.7, ctx="loop"),
    Rule("PY-SLEEP-ASYNC", r"\btime\.sleep\(", "time.sleep() trong coroutine chặn cả event loop",
         "Trong `async def`, time.sleep() dừng toàn bộ event loop: mọi request/tác vụ khác đứng im trong lúc chờ.",
         "await asyncio.sleep(...)", "medium", "bug", 0.85, ctx="async"),
    Rule("PY-BLOCKING-ASYNC", r"\brequests\.(get|post|put|patch|delete|head|request)\(|\burllib\.request\.urlopen\(",
         "Gọi mạng đồng bộ trong coroutine", "requests/urlopen chặn event loop suốt thời gian chờ mạng; async không còn tác dụng.",
         "httpx.AsyncClient / aiohttp, hoặc await loop.run_in_executor(None, ...)", "medium", "bug", 0.75, ctx="async"),
]

JS_RULES = [
    Rule("JS-NEW-BUFFER", r"\bnew\s+Buffer\(", "new Buffer() đã deprecated vì không an toàn", "Buffer(số) cấp vùng nhớ chưa xoá, Buffer(chuỗi) thì không: dữ liệu đầu vào quyết định hành vi (DEP0005).",
         "Buffer.from(...) / Buffer.alloc(n)", "medium", "security", 0.9),
    Rule("JS-REQUEST-PKG", r"require\(\s*['\"]request(?:-promise(?:-native)?)?['\"]\s*\)|from\s+['\"]request['\"]", "Thư viện `request` đã ngừng phát triển từ 2020",
         "Không còn bản vá bảo mật; phụ thuộc kéo theo nhiều gói lỗi thời.", "fetch (Node 18+), undici, axios hoặc got", "medium", "maintainability", 0.9),
    Rule("JS-DOCUMENT-WRITE", r"\bdocument\.write(?:ln)?\(", "document.write() đã lỗi thời và dễ XSS", "Bị trình duyệt chặn khi gọi sau khi tải xong, và chèn thẳng HTML chưa escape.",
         "Tạo phần tử bằng DOM API hoặc textContent.", "medium", "security", 0.8),
    Rule("JS-REACT-LIFECYCLE", r"\b(componentWillMount|componentWillReceiveProps|componentWillUpdate)\s*\(", "Lifecycle React `{0}` đã deprecated",
         "Bị đổi tên thành UNSAFE_* từ React 16.3 và không tương thích với Strict Mode / concurrent rendering.",
         "componentDidMount / getDerivedStateFromProps / componentDidUpdate, hoặc hooks", "medium", "maintainability", 0.9),
    Rule("JS-REACTDOM-RENDER", r"\bReactDOM\.(render|hydrate)\(", "ReactDOM.{0}() đã deprecated từ React 18", "Chạy ở chế độ React 17 (không concurrent) và bị xoá ở React 19.",
         "createRoot(el).render(<App/>) / hydrateRoot(el, <App/>) từ react-dom/client", "medium", "maintainability", 0.9),
    Rule("JS-FINDDOMNODE", r"\bfindDOMNode\(", "findDOMNode() đã bị xoá từ React 19", "Ném lỗi trên React 19.", "Dùng ref trên phần tử cần truy cập.", "medium", "bug", 0.9),
    Rule("JS-REACT-PROPTYPES", r"\bReact\.PropTypes\b", "React.PropTypes đã bị xoá từ React 16", "undefined -> TypeError khi khai báo propTypes.",
         "import PropTypes from 'prop-types'", "high", "bug", 0.9),
    Rule("JS-FS-EXISTS", r"\bfs\.exists\(", "fs.exists() đã deprecated (DEP0013)", "Callback không theo chuẩn (err, result) nên không promisify được; dễ TOCTOU.",
         "fs.promises.access() hoặc fs.existsSync()", "medium", "maintainability", 0.9),
    Rule("JS-URL-PARSE", r"\burl\.parse\(", "url.parse() là API legacy đã deprecated", "Không theo chuẩn WHATWG và từng có lỗ hổng hostname spoofing.",
         "new URL(str)", "medium", "maintainability", 0.8),
    Rule("JS-CREATECIPHER", r"\bcrypto\.create(?:De)?[Cc]ipher\(", "crypto.createCipher/createDecipher đã bị xoá (Node 22)",
         "Dẫn xuất khoá bằng MD5 không salt, không an toàn; hàm không còn tồn tại trên Node 22+.",
         "crypto.createCipheriv(alg, key, iv) với khoá từ scrypt/pbkdf2", "high", "security", 0.95),
    Rule("JS-UTIL-IS", r"\butil\.is(Array|Object|String|Number|Boolean|Function|RegExp|Date|Error|Null|Undefined|Primitive|Buffer)\(",
         "util.is{0}() đã bị xoá khỏi Node", "Các hàm util.isX bị xoá từ Node 23 -> TypeError.", "Array.isArray / typeof / instanceof", "medium", "bug", 0.85),
    Rule("JS-GETYEAR", r"\.getYear\(\)", "Date.getYear() trả về năm trừ 1900", "Lỗi thời từ ES3; hầu như luôn là bug (2026 -> 126).", ".getFullYear()", "medium", "bug", 0.9),
    Rule("JS-ARGUMENTS-CALLEE", r"\barguments\.callee\b", "arguments.callee bị cấm trong strict mode", "TypeError trong module ES / strict mode.",
         "Đặt tên cho function expression và gọi theo tên.", "medium", "bug", 0.9),
    Rule("JS-SYNC-XHR", r"\.open\(\s*['\"](?:GET|POST|PUT|DELETE|PATCH)['\"]\s*,[^)]*,\s*false\s*\)", "XMLHttpRequest đồng bộ đóng băng giao diện",
         "Trình duyệt đã deprecated XHR đồng bộ trên luồng chính; UI treo cho tới khi mạng trả về.", "fetch() với async/await", "medium", "performance", 0.9),
    Rule("JS-FOREACH-ASYNC", r"\.forEach\(\s*async\b", "forEach(async ...) không chờ các await bên trong",
         "forEach bỏ qua Promise trả về: code sau vòng lặp chạy trước khi các tác vụ xong, lỗi bên trong thành unhandled rejection.",
         "for (const x of xs) { await ... }  hoặc  await Promise.all(xs.map(async ...))", "medium", "bug", 0.85),
    Rule("JS-SUBSTR", r"\.substr\(", "String.substr() đã deprecated (Annex B)", "Chỉ còn vì tương thích; dễ nhầm với substring.", ".slice(start, end) / .substring()", "low", "maintainability", 0.8),
    Rule("JS-ESCAPE", r"(?<![\w.])(?:escape|unescape)\(", "escape()/unescape() đã deprecated", "Không xử lý đúng Unicode ngoài Latin-1.", "encodeURIComponent / decodeURIComponent", "low", "maintainability", 0.75),
    Rule("JS-KEYCODE", r"\.keyCode\b", "KeyboardEvent.keyCode đã deprecated", "Giá trị khác nhau giữa bàn phím / trình duyệt.", "event.key hoặc event.code", "low", "maintainability", 0.8),
    Rule("JS-JSON-DEEPCLONE", r"\bJSON\.parse\(\s*JSON\.stringify\(", "Deep clone bằng JSON.parse(JSON.stringify())", "Mất Date/Map/Set/undefined/hàm và chậm với object lớn.",
         "structuredClone(obj)", "low", "performance", 0.8),
    Rule("JS-MOMENT", r"require\(\s*['\"]moment['\"]\s*\)|from\s+['\"]moment['\"]", "moment.js ở chế độ bảo trì, không còn phát triển",
         "Chính dự án khuyên không dùng cho dự án mới: bundle lớn (~300KB với locale), object khả biến.",
         "dayjs / date-fns / Luxon / Temporal", "low", "maintainability", 0.75),
]

GO_RULES = [
    Rule("GO-IOUTIL", r"\bioutil\.(ReadAll|ReadFile|WriteFile|ReadDir|TempFile|TempDir|NopCloser|Discard)\b", "io/ioutil.{0} đã deprecated từ Go 1.16",
         "Gói ioutil chỉ còn là wrapper; go vet / staticcheck báo SA1019.",
         "io.ReadAll / os.ReadFile / os.WriteFile / os.ReadDir / os.CreateTemp / os.MkdirTemp / io.NopCloser / io.Discard", "medium", "maintainability", 0.9),
    Rule("GO-RAND-SEED", r"\brand\.Seed\(", "rand.Seed() đã deprecated từ Go 1.20", "Bộ sinh toàn cục đã tự seed ngẫu nhiên; Seed(time.Now()...) là thừa và yếu nếu dùng cho bảo mật.",
         "Bỏ Seed(); cần tái lập thì rand.New(rand.NewSource(seed)); cần bảo mật thì crypto/rand.", "medium", "maintainability", 0.9),
    Rule("GO-STRINGS-TITLE", r"\bstrings\.Title\(", "strings.Title() đã deprecated từ Go 1.18", "Không xử lý đúng Unicode.", "golang.org/x/text/cases: cases.Title(language.Und).String(s)", "medium", "maintainability", 0.85),
    Rule("GO-ERRORS-SPRINTF", r"\berrors\.New\(\s*fmt\.Sprintf\(", "errors.New(fmt.Sprintf(...)) thay vì fmt.Errorf", "Dài dòng và không bọc được lỗi gốc bằng %w.", "fmt.Errorf(\"...: %w\", err)", "low", "maintainability", 0.9),
    Rule("GO-XNET-CONTEXT", r"\"golang\.org/x/net/context\"", "golang.org/x/net/context đã được thay bằng gói chuẩn context", "Gói chuẩn từ Go 1.7; bản x/net chỉ là alias.", "import \"context\"", "medium", "maintainability", 0.9),
    Rule("GO-GRPC-INSECURE", r"\bgrpc\.WithInsecure\(\)", "grpc.WithInsecure() đã deprecated", "", "grpc.WithTransportCredentials(insecure.NewCredentials())", "medium", "maintainability", 0.85),
    Rule("GO-JWT-GO", r"\"github\.com/dgrijalva/jwt-go", "dgrijalva/jwt-go không còn bảo trì và có CVE-2020-26160", "Lỗ hổng bỏ qua kiểm tra audience; dự án đã ngừng từ 2020.",
         "github.com/golang-jwt/jwt/v5", "high", "security", 0.95),
    Rule("GO-SATORI-UUID", r"\"github\.com/satori/go\.uuid", "satori/go.uuid có CVE-2021-3538 (UUID đoán được)", "Bản v1.2.0 trở xuống sinh UUID v4 từ nguồn ngẫu nhiên yếu.",
         "github.com/google/uuid hoặc github.com/gofrs/uuid", "medium", "security", 0.85),
    Rule("GO-GOLANG-PROTOBUF", r"\"github\.com/golang/protobuf/", "github.com/golang/protobuf đã deprecated", "APIv1; bản mới là google.golang.org/protobuf.", "google.golang.org/protobuf/proto", "medium", "maintainability", 0.85),
    Rule("GO-TIME-NOW-SUB", r"\btime\.Now\(\)\.Sub\(", "time.Now().Sub(t) viết gọn bằng time.Since(t)", "", "time.Since(t)", "low", "maintainability", 0.9),
]

JAVA_RULES = [
    Rule("JAVA-BOXING-CTOR", r"\bnew\s+(Integer|Long|Double|Float|Short|Byte|Boolean|Character)\s*\(", "new {0}(...) đã deprecated for removal (Java 9+)",
         "Constructor wrapper luôn cấp phát object mới, bỏ qua cache; bị đánh dấu xoá.", "{0}.valueOf(...)", "medium", "maintainability", 0.9),
    Rule("JAVA-THREAD-STOP", r"\.(stop|suspend|resume)\(\)\s*;", "Thread.{0}() ném UnsupportedOperationException từ Java 20", "Bị deprecated từ lâu vì làm hỏng bất biến; Java 20+ ném lỗi luôn.",
         "Cờ volatile / interrupt() và kiểm tra isInterrupted().", "high", "bug", 0.8),
    Rule("JAVA-FINALIZE", r"\bprotected\s+void\s+finalize\s*\(", "finalize() đã deprecated for removal (JEP 421)", "Không bảo đảm được gọi, làm chậm GC.",
         "try-with-resources / java.lang.ref.Cleaner", "medium", "maintainability", 0.85),
    Rule("JAVA-NEWINSTANCE", r"\.class\.newInstance\(\)|forName\([^)]*\)\.newInstance\(\)", "Class.newInstance() đã deprecated (Java 9)", "Ném exception đã kiểm tra bị che giấu.",
         ".getDeclaredConstructor().newInstance()", "medium", "maintainability", 0.85),
    Rule("JAVA-JAXB", r"\bimport\s+javax\.xml\.bind\b", "javax.xml.bind (JAXB) đã bị xoá khỏi JDK 11", "ClassNotFoundException khi chạy trên JDK 11+ nếu không thêm dependency.",
         "jakarta.xml.bind-api + glassfish jaxb-runtime", "medium", "bug", 0.85),
    Rule("JAVA-BIGDECIMAL-ROUND", r"\bBigDecimal\.ROUND_[A-Z_]+\b", "BigDecimal.ROUND_* đã deprecated (Java 9)", "", "RoundingMode.HALF_UP...", "medium", "maintainability", 0.9),
    Rule("JAVA-SUN-MISC", r"\bsun\.misc\.\w+", "Dùng API nội bộ sun.misc.*", "Không phải API công khai; bị đóng dần từ Java 9 (module encapsulation).", "Dùng API chuẩn tương đương (VarHandle, Base64...).", "medium", "maintainability", 0.8),
    Rule("JAVA-SECURITYMANAGER", r"\bSecurityManager\b", "SecurityManager đã deprecated for removal (JEP 411)", "", "", "medium", "maintainability", 0.75),
    Rule("JAVA-LOG4J1", r"\bimport\s+org\.apache\.log4j\.", "Log4j 1.x đã EOL từ 2015 và có nhiều CVE", "CVE-2019-17571, CVE-2021-4104... không được vá.", "Log4j 2 / SLF4J + Logback", "high", "security", 0.9),
    Rule("JAVA-LEGACY-DATE", r"\bnew\s+SimpleDateFormat\(|\bCalendar\.getInstance\(\)", "API ngày giờ cũ (SimpleDateFormat/Calendar)", "Không thread-safe, API khả biến; java.time có từ Java 8.",
         "java.time (LocalDate, ZonedDateTime, DateTimeFormatter)", "low", "maintainability", 0.8),
    Rule("JAVA-LEGACY-COLLECTION", r"\bnew\s+(Vector|Hashtable|StringBuffer)\s*[<(]", "{0} là lớp legacy đồng bộ hoá thừa", "Khoá mọi thao tác dù không chia sẻ giữa luồng.",
         "ArrayList / HashMap / StringBuilder", "low", "performance", 0.8),
]

DOCKER_RULES = [
    Rule("DOCKER-MAINTAINER", r"^\s*MAINTAINER\b", "Lệnh MAINTAINER đã deprecated", "", "LABEL org.opencontainers.image.authors=\"...\"", "medium", "devops", 0.95),
    Rule("DOCKER-APT-KEY", r"\bapt-key\s+(add|adv)\b", "apt-key đã deprecated (Debian 11 / Ubuntu 22.04)", "Khoá thêm bằng apt-key được tin cho MỌI repo; lệnh sẽ bị xoá.",
         "Tải khoá vào /etc/apt/keyrings/ và dùng [signed-by=...] trong sources.list", "medium", "devops", 0.85),
]

CI_RULES = [
    Rule("CI-SET-ENV", r"::(set-env|add-path)\b", "Lệnh workflow ::{0} đã bị GitHub tắt từ 11/2020", "Bước này không còn tác dụng và làm job lỗi.",
         "echo \"NAME=value\" >> \"$GITHUB_ENV\"  /  echo \"/path\" >> \"$GITHUB_PATH\"", "high", "devops", 0.95),
    Rule("CI-SET-OUTPUT", r"::(set-output|save-state)\b", "Lệnh workflow ::{0} đã deprecated (10/2022)", "GitHub cảnh báo và sẽ tắt; output có thể không truyền được.",
         "echo \"name=value\" >> \"$GITHUB_OUTPUT\"  /  >> \"$GITHUB_STATE\"", "medium", "devops", 0.95),
    Rule("CI-ARTIFACT-V3", r"uses:\s*actions/(upload-artifact|download-artifact)@v[123]\b", "actions/{0}@v1-v3 đã bị tắt từ 30/01/2025", "Job dùng phiên bản này thất bại ngay.",
         "actions/{0}@v4", "high", "devops", 0.95),
    Rule("CI-ACTION-V12", r"uses:\s*actions/(checkout|setup-python|setup-node|setup-java|setup-go|cache|github-script)@v[12]\b", "actions/{0}@v1/v2 chạy trên Node 12 đã bị gỡ",
         "Runner mới không còn Node 12/16 cho action; job thất bại hoặc bị ép chạy Node 20.", "actions/{0}@v4 (hoặc bản mới nhất)", "high", "devops", 0.9),
    Rule("CI-ACTION-V3", r"uses:\s*actions/(checkout|setup-python|setup-node|setup-java|setup-go|cache|github-script)@v3\b", "actions/{0}@v3 (Node 16) đã deprecated",
         "Node 16 cho action bị loại bỏ; GitHub cảnh báo và sẽ dừng.", "actions/{0}@v4", "medium", "devops", 0.85),
    Rule("CI-CREATE-RELEASE", r"uses:\s*actions/create-release@", "actions/create-release đã bị lưu trữ (archived)", "", "softprops/action-gh-release hoặc gh release create", "medium", "devops", 0.85),
]

# Thư viện bị bỏ rơi trong manifest: tên (chữ thường) -> (tiêu đề, cách sửa, severity, category, confidence)
PYPI_ABANDONED = {
    "pycrypto": ("pycrypto ngừng bảo trì từ 2013, có CVE-2013-7459 / CVE-2018-6594", "pycryptodome (API tương thích) hoặc cryptography", "high", "security", 0.95),
    "nose": ("nose ngừng phát triển từ 2015, không chạy trên Python 3.12", "pytest", "medium", "maintainability", 0.9),
    "sklearn": ("Gói `sklearn` trên PyPI chỉ là gói giả, pip đã chặn cài", "scikit-learn", "medium", "bug", 0.95),
    "pil": ("Gói PIL không còn tồn tại trên PyPI", "Pillow", "high", "bug", 0.9),
    "futures": ("`futures` là backport cho Python 2, trên Python 3 đã có concurrent.futures", "bỏ khỏi manifest", "medium", "maintainability", 0.85),
    "enum34": ("`enum34` là backport cho Python 2, trên 3.4+ làm hỏng module enum chuẩn", "bỏ khỏi manifest", "medium", "bug", 0.85),
    "typing": ("`typing` trên PyPI là backport cũ, trên Python 3.5+ gây xung đột với module chuẩn", "bỏ khỏi manifest", "medium", "bug", 0.85),
    "pathlib": ("`pathlib` trên PyPI là backport cũ, Python 3 đã có sẵn", "bỏ khỏi manifest", "medium", "maintainability", 0.85),
    "pathlib2": ("`pathlib2` là backport cho Python 2", "bỏ khỏi manifest", "medium", "maintainability", 0.8),
    "asyncio": ("`asyncio` trên PyPI là bản cũ cho Python 3.3, làm hỏng asyncio chuẩn", "bỏ khỏi manifest", "medium", "bug", 0.85),
    "trollius": ("trollius (asyncio cho Python 2) đã ngừng", "bỏ khỏi manifest", "medium", "maintainability", 0.85),
    "ipaddress": ("`ipaddress` backport Python 2, Python 3 có sẵn", "bỏ khỏi manifest", "low", "maintainability", 0.8),
    "flask-script": ("Flask-Script ngừng bảo trì, không tương thích Flask 2+", "flask CLI (click)", "medium", "maintainability", 0.85),
    "distribute": ("`distribute` đã nhập lại vào setuptools từ 2013", "setuptools", "medium", "maintainability", 0.9),
    "bs4": ("Gói `bs4` trên PyPI chỉ là gói giả bọc beautifulsoup4", "beautifulsoup4", "low", "maintainability", 0.8),
}
NPM_ABANDONED = {
    "request": ("`request` ngừng phát triển từ 2020", "fetch / undici / axios / got", "medium", "maintainability", 0.9),
    "request-promise": ("`request-promise` ngừng cùng `request`", "axios / got", "medium", "maintainability", 0.9),
    "request-promise-native": ("`request-promise-native` ngừng cùng `request`", "axios / got", "medium", "maintainability", 0.9),
    "node-sass": ("node-sass đã deprecated (LibSass ngừng), không build được trên Node mới", "sass (Dart Sass)", "medium", "maintainability", 0.9),
    "tslint": ("TSLint đã deprecated từ 2019", "eslint + typescript-eslint", "medium", "maintainability", 0.9),
    "node-uuid": ("node-uuid đã đổi tên", "uuid", "medium", "maintainability", 0.9),
    "left-pad": ("left-pad không còn cần thiết", "String.prototype.padStart", "low", "maintainability", 0.9),
    "@babel/polyfill": ("@babel/polyfill đã deprecated từ Babel 7.4", "core-js + regenerator-runtime, hoặc useBuiltIns của preset-env", "medium", "maintainability", 0.9),
    "babel-preset-es2015": ("babel-preset-es2015 đã deprecated", "@babel/preset-env", "medium", "maintainability", 0.9),
    "babel-preset-es2016": ("babel-preset-es2016 đã deprecated", "@babel/preset-env", "medium", "maintainability", 0.9),
    "babel-preset-es2017": ("babel-preset-es2017 đã deprecated", "@babel/preset-env", "medium", "maintainability", 0.9),
    "gulp-util": ("gulp-util đã deprecated", "các gói nhỏ riêng (ansi-colors, fancy-log, plugin-error...)", "medium", "maintainability", 0.9),
    "istanbul": ("istanbul (CLI) đã deprecated", "nyc hoặc c8", "medium", "maintainability", 0.85),
    "protractor": ("Protractor đã EOL từ 2023", "Playwright / Cypress / WebdriverIO", "medium", "maintainability", 0.9),
    "karma": ("Karma đã deprecated từ 2023", "Jest / Vitest / Web Test Runner", "medium", "maintainability", 0.85),
    "bower": ("Bower đã ngừng từ 2017", "npm / yarn / pnpm", "medium", "maintainability", 0.9),
    "coffee-script": ("Gói coffee-script đã đổi tên", "coffeescript", "medium", "maintainability", 0.9),
    "phantomjs": ("PhantomJS ngừng phát triển từ 2018", "Playwright / Puppeteer", "medium", "maintainability", 0.9),
    "phantomjs-prebuilt": ("PhantomJS ngừng phát triển từ 2018", "Playwright / Puppeteer", "medium", "maintainability", 0.9),
    "crypto": ("Gói `crypto` trên npm chỉ là gói rỗng đã deprecated", "module crypto có sẵn của Node", "medium", "bug", 0.9),
    "querystring": ("`querystring` trên npm là bản legacy", "URLSearchParams", "low", "maintainability", 0.8),
    "optimist": ("optimist đã deprecated", "yargs", "low", "maintainability", 0.85),
    "faker": ("`faker` bị tác giả phá hoại ở bản 6.6.6 và không còn bảo trì", "@faker-js/faker", "medium", "security", 0.85),
    "event-stream": ("event-stream từng bị chèn mã độc (3.3.6) và không còn bảo trì", "through2 / readable-stream", "high", "security", 0.85),
    "moment": ("moment.js ở chế độ bảo trì, bundle lớn", "dayjs / date-fns / Luxon", "low", "maintainability", 0.75),
    "uglify-js": ("uglify-js không hỗ trợ ES2015+ (chỉ ES5)", "terser", "low", "maintainability", 0.75),
}
GO_ABANDONED = {
    "github.com/dgrijalva/jwt-go": ("dgrijalva/jwt-go không còn bảo trì, CVE-2020-26160", "github.com/golang-jwt/jwt/v5", "high", "security", 0.95),
    "github.com/satori/go.uuid": ("satori/go.uuid có CVE-2021-3538", "github.com/google/uuid", "medium", "security", 0.85),
    "github.com/golang/protobuf": ("github.com/golang/protobuf đã deprecated", "google.golang.org/protobuf", "medium", "maintainability", 0.85),
    "github.com/pkg/errors": ("pkg/errors đã lưu trữ; errors chuẩn có %w / errors.Is / errors.As", "errors + fmt.Errorf(\"%w\")", "low", "maintainability", 0.8),
    "golang.org/x/net/context": ("x/net/context chỉ là alias của context chuẩn", "context", "medium", "maintainability", 0.9),
}
JAVA_ABANDONED = {   # khoá: artifactId (pom) hoặc group:artifact (gradle)
    "log4j": ("Log4j 1.x đã EOL từ 2015 với nhiều CVE không vá", "org.apache.logging.log4j:log4j-core 2.x", "high", "security", 0.9),
    "log4j:log4j": ("Log4j 1.x đã EOL từ 2015 với nhiều CVE không vá", "org.apache.logging.log4j:log4j-core 2.x", "high", "security", 0.9),
    "commons-lang": ("commons-lang 2.x đã EOL", "org.apache.commons:commons-lang3", "low", "maintainability", 0.85),
    "commons-lang:commons-lang": ("commons-lang 2.x đã EOL", "org.apache.commons:commons-lang3", "low", "maintainability", 0.85),
    "commons-collections": ("commons-collections 3.x có CVE-2015-6420 (deserialization RCE)", "org.apache.commons:commons-collections4", "high", "security", 0.85),
    "commons-collections:commons-collections": ("commons-collections 3.x có CVE-2015-6420", "org.apache.commons:commons-collections4", "high", "security", 0.85),
    "javax.xml.bind:jaxb-api": ("javax.xml.bind đã chuyển sang jakarta", "jakarta.xml.bind:jakarta.xml.bind-api", "low", "maintainability", 0.8),
    "org.codehaus.jackson:jackson-mapper-asl": ("Jackson 1.x đã EOL, có CVE", "com.fasterxml.jackson.core:jackson-databind", "high", "security", 0.9),
    "jackson-mapper-asl": ("Jackson 1.x đã EOL, có CVE", "com.fasterxml.jackson.core:jackson-databind", "high", "security", 0.9),
    "junit": ("JUnit 4 ở chế độ bảo trì", "org.junit.jupiter:junit-jupiter (JUnit 5)", "low", "maintainability", 0.7),
}

# ---------------------------------------------------------------- phân loại file

_DOC_EXT = (".md", ".rst", ".txt", ".adoc", ".csv", ".svg", ".lock", ".min.js", ".map")
_DOC_NAMES = ("license", "changelog", "changes", "history", "notice", "authors")


def _kinds(path: str) -> set[str]:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    low = base.lower()
    k: set[str] = set()
    if low.endswith((".py", ".pyi")):
        k.add("python")
    if low.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")):
        k.add("js")
    if low.endswith(".go"):
        k.add("go")
    if low.endswith((".java", ".kt", ".scala")):
        k.add("java")
    if low.startswith("dockerfile") or low.endswith(".dockerfile") or (low.startswith(("docker-compose", "compose")) and low.endswith((".yml", ".yaml"))):
        k.add("docker")
    if low.endswith((".yml", ".yaml")):
        k.add("yaml")
        if "/.github/workflows/" in "/" + p or low == ".gitlab-ci.yml":
            k.add("ci")
    if (low.startswith("requirements") and low.endswith(".txt")) or low in ("pyproject.toml", "setup.py", "setup.cfg", "pipfile", "constraints.txt"):
        k.add("deps_py")
    if low == "package.json":
        k.add("deps_js")
    if low == "go.mod":
        k.add("deps_go")
    if low == "pom.xml" or low.startswith("build.gradle"):
        k.add("deps_java")
    if low in (".python-version", "runtime.txt", ".nvmrc", ".node-version", ".go-version", ".ruby-version", ".tool-versions"):
        k.add("runtime_file")
    if low.endswith((".sh", ".bash", ".ps1")) or low == "makefile":
        k.add("shell")
    return k


def _is_doc(path: str) -> bool:
    low = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if low.startswith("requirements") and low.endswith(".txt"):
        return False
    if low == "runtime.txt":
        return False
    return low.endswith(_DOC_EXT) or low.split(".")[0] in _DOC_NAMES


def _is_comment(text: str, kinds: set[str]) -> bool:
    s = text.lstrip()
    if kinds & {"python", "yaml", "docker", "shell", "deps_py", "runtime_file"}:
        return s.startswith("#")
    if kinds & {"js", "go", "java"}:
        return s.startswith(("//", "/*", "*"))
    return False


# ---------------------------------------------------------------- đọc nội dung

def _read_lines(repo_dir: str, cf: ChangedFile) -> tuple[list[tuple[int, str]], bool]:
    """[(số dòng, nội dung)] của bản head: đọc từ đĩa nếu có (cả file), không thì từ hunk của diff."""
    path = os.path.join(repo_dir, cf.path) if repo_dir else ""
    if path and os.path.isfile(path):
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                return [], False
            with open(path, "rb") as fh:
                raw = fh.read()
            if b"\0" in raw[:8000]:
                return [], False
            text = raw.decode("utf-8", errors="replace")
            return list(enumerate(text.splitlines(), start=1)), True
        except OSError:        # không đọc được: dùng hunk của diff
            pass
    return sorted(cf.new_lines.items()), False


# ---------------------------------------------------------------- nhóm 1: khoá / chứng chỉ hết hạn

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)
# (?<![a-z]) / (?![a-z]) thay cho \b: tên biến kiểu LICENSE_KEY / KEY_VALID_UNTIL có dấu '_' làm \b không khớp.
EXPIRY_WORD_RE = re.compile(r"(?i)(?<![a-z])(expir\w*|valid[_ -]?(?:until|to|till|through)|not[_ -]?after|hết hạn|deadline|sunset|"
                            r"end[_ -]?of[_ -]?(?:life|support)|remove[_ -]?(?:after|by|before)|until)(?![a-z])")
KEYISH_RE = re.compile(r"(?i)(?<![a-z])(api[_-]?key|secret|token|credential|password|passwd|cert(?:ificate)?s?|licen[cs]e|key|jwt|cookie|session)(?![a-z])")
ISO_DATE_RE = re.compile(r"\b(20\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])\b")
EPOCH_RE = re.compile(r"\b(1[5-9]\d{8})\b")
IGNORE_WORDS = ("example", "placeholder", "dummy", "sample", "changeme", "xxx")


def _jwt_exp(token: str) -> datetime | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    if exp > 1e12:            # mili-giây
        exp /= 1000
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _cert_not_after(pem: str) -> datetime | None:
    try:
        from cryptography import x509
    except ImportError:        # không có cryptography: bỏ qua kiểm tra chứng chỉ
        return None
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("ascii", errors="ignore"))
    except Exception:
        return None
    na = getattr(cert, "not_valid_after_utc", None)
    if na is None:
        na = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return na


def _dates_in(text: str) -> list[date]:
    out = []
    for m in ISO_DATE_RE.finditer(text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return out


def _mk(cf: ChangedFile, line: int, text: str, rid: str, title: str, expl: str, fix: str, sev: str, cat: str, conf: float) -> Finding:
    return Finding(file=cf.path, line=max(1, line), severity=sev, category=cat, title=f"{rid}: {title}", explanation=expl,
                   suggested_fix=fix, confidence=conf, role=ROLE, source=SOURCE, existing_code=text.strip()[:200], anchor="tool")


def _credential_checks(cf: ChangedFile, lines: list[tuple[int, str]], full_text: str, today: date, warn_days: int) -> list[Finding]:
    out: list[Finding] = []
    now = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    # JWT có exp
    for no, text in lines:
        if any(w in text.lower() for w in IGNORE_WORDS):
            continue
        for m in JWT_RE.finditer(text):
            exp = _jwt_exp(m.group(0))
            if exp is None:
                continue
            left = (exp - now).days
            if left < 0:
                out.append(_mk(cf, no, text, "KEY-JWT", f"Token JWT hard-code đã hết hạn từ {exp.date()}",
                               f"Token này có claim `exp` = {exp.date()} (đã qua {-left} ngày). Mọi lời gọi dùng nó sẽ bị từ chối 401, "
                               "và một token thật nằm trong mã nguồn vẫn là bí mật bị lộ.",
                               "Xoá token khỏi code; cấp token mới và nạp qua biến môi trường / secret manager.", "high", "security", 0.9))
            elif left <= warn_days:
                out.append(_mk(cf, no, text, "KEY-JWT", f"Token JWT hard-code sắp hết hạn ({exp.date()}, còn {left} ngày)",
                               f"Claim `exp` = {exp.date()}; sau ngày đó code dùng token này sẽ lỗi 401.",
                               "Chuyển token sang biến môi trường / secret manager và có quy trình xoay vòng.", "medium", "security", 0.8))
    # chứng chỉ PEM (có thể nhiều dòng)
    for m in CERT_RE.finditer(full_text):
        na = _cert_not_after(m.group(0))
        if na is None:
            continue
        idx = full_text.count("\n", 0, m.start())
        line_no, text = lines[idx] if idx < len(lines) else (idx + 1, "")
        left = (na - now).days
        if left < 0:
            out.append(_mk(cf, line_no, text, "KEY-CERT", f"Chứng chỉ đã hết hạn từ {na.date()}",
                           f"notAfter = {na.date()} (đã qua {-left} ngày). TLS handshake / xác minh chữ ký với chứng chỉ này sẽ thất bại.",
                           "Cấp lại chứng chỉ và nạp từ secret store thay vì nhúng vào mã nguồn.", "high", "devops", 0.9))
        elif left <= warn_days:
            out.append(_mk(cf, line_no, text, "KEY-CERT", f"Chứng chỉ sắp hết hạn ({na.date()}, còn {left} ngày)",
                           f"notAfter = {na.date()}.", "Lên lịch gia hạn / xoay chứng chỉ trước ngày hết hạn.", "medium", "devops", 0.8))
    # dòng ghi hạn dùng của khoá / token / chứng chỉ
    for no, text in lines:
        if len(text) > 400 or any(w in text.lower() for w in IGNORE_WORDS):
            continue
        if not EXPIRY_WORD_RE.search(text) or not KEYISH_RE.search(text):
            continue
        dates = _dates_in(text)
        if not dates:
            dates = [datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).date() for m in EPOCH_RE.finditer(text)]
        for d in dates[:1]:
            left = (d - today).days
            if left < 0:
                out.append(_mk(cf, no, text, "KEY-DATE", f"Khoá/chứng chỉ ghi hạn dùng {d} đã qua",
                               f"Dòng này ghi hạn dùng của một khoá/token/chứng chỉ là {d} (đã qua {-left} ngày); thứ nó bảo vệ nhiều khả năng không còn dùng được.",
                               "Xoay khoá và cập nhật (hoặc bỏ) ghi chú hạn dùng; cân nhắc đưa khoá ra khỏi mã nguồn.", "high", "security", 0.75))
            elif left <= warn_days:
                out.append(_mk(cf, no, text, "KEY-DATE", f"Khoá/chứng chỉ sắp hết hạn ({d}, còn {left} ngày)",
                               f"Dòng này ghi hạn dùng {d}.", "Lên lịch xoay khoá trước ngày hết hạn.", "medium", "security", 0.7))
    return out


# ---------------------------------------------------------------- nhóm 2: code lỗi thời / không tối ưu

IMAGE_RE = re.compile(r"^\s*(?:FROM|-?\s*image:)\s+[\"']?(?:--platform=\S+\s+)?([\w./-]+?)(?::([\w.-]+))?(?:@sha256:[0-9a-f]+)?[\"']?(?:\s+(?:AS|as)\s+\S+)?\s*$")
RUNS_ON_RE = re.compile(r"runs-on:\s*[\[\"']?\s*((?:ubuntu|windows|macos)-[\d.]+)")
PY_VER_KEY_RE = re.compile(r"python[-_]?version\s*[:=]")
NODE_VER_KEY_RE = re.compile(r"node[-_]?version\s*[:=]")
GO_VER_KEY_RE = re.compile(r"go[-_]?version\s*[:=]")
DJANGO_PIN_RE = re.compile(r"(?i)^\s*[\"']?django\s*==\s*(\d+\.\d+)")
PY_REQ_RE = re.compile(r"^\s*[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[=<>!~;\[\"'@]|$)")
NPM_DEP_RE = re.compile(r"^\s*\"((?:@[\w.-]+/)?[\w.-]+)\"\s*:\s*\"[^\"]*\"")
GO_REQ_RE = re.compile(r"^\s*((?:github\.com|golang\.org|gopkg\.in)/\S+)\s+v")
POM_ART_RE = re.compile(r"<artifactId>\s*([\w.-]+)\s*</artifactId>")
GRADLE_DEP_RE = re.compile(r"[\"']([\w.-]+):([\w.-]+):[^\"']*[\"']")
COMPARE_RE = re.compile(r"(?<![<>=!\-])(?:<=?|>=?)(?![<>=])|\.(?:before|after|isBefore|isAfter|Before|After)\(")
DATE_CTOR_RES = [
    (re.compile(r"\b(?:datetime|date)\(\s*(20\d{2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})"), 0),
    (re.compile(r"\bnew\s+Date\(\s*(20\d{2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})"), 1),      # JS: tháng đếm từ 0
    (re.compile(r"\bnew\s+Date\(\s*[\"'](20\d{2})-(\d{2})-(\d{2})"), 0),
    (re.compile(r"\bLocalDate\.of\(\s*(20\d{2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})"), 0),
    (re.compile(r"\btime\.Date\(\s*(20\d{2})\s*,\s*(?:time\.)?(\w+)\s*,\s*(\d{1,2})"), 2),  # Go: tên tháng
]
GO_MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                         "September", "October", "November", "December"), start=1)}


def _image_findings(cf, no, text, today) -> list[Finding]:
    m = IMAGE_RE.match(text)
    if not m or "$" in text:
        return []
    name, tag = m.group(1), m.group(2)
    if not tag or tag == "latest":
        return []
    short = name.split("/")[-1]
    checks: list[tuple[str, dict, str]] = []
    if short in IMAGE_TABLES:
        label, table, parts, names = IMAGE_TABLES[short]
        vm = re.match(r"(\d+(?:\.\d+)*)", tag)
        key = _version_key(vm.group(1), parts) if vm else names.get(tag.split("-")[0], "")
        if key:
            checks.append((label, table, key))
    # hậu tố hệ điều hành trong tag: python:3.9-slim-bullseye, node:18-alpine3.17, ruby:3.1-focal
    for piece in tag.split("-")[1:]:
        if piece in DEBIAN_NAMES:
            checks.append(("Debian", DEBIAN_EOL, DEBIAN_NAMES[piece]))
        elif piece in UBUNTU_NAMES:
            checks.append(("Ubuntu", UBUNTU_EOL, UBUNTU_NAMES[piece]))
        elif (am := re.match(r"alpine(\d+\.\d+)", piece)):
            checks.append(("Alpine", ALPINE_EOL, am.group(1)))
    out = []
    for label, table, key in checks:
        st, eol = eol_status(table, key, today)
        if st == "expired":
            newest = latest_supported(table, today)
            out.append(_mk(cf, no, text, "EOL-IMAGE", f"Image `{name}:{tag}` dùng {label} {key} đã hết hạn hỗ trợ từ {eol}",
                           f"{label} {key} không còn nhận bản vá bảo mật từ {eol}; image này mang theo lỗ hổng đã biết và không được sửa.",
                           f"Nâng lên {label} {newest} (ví dụ `{short}:{newest}`)" if newest else "Nâng lên phiên bản còn được hỗ trợ.",
                           "medium", "devops", 0.9))
        elif st == "soon":
            out.append(_mk(cf, no, text, "EOL-IMAGE", f"Image `{name}:{tag}`: {label} {key} sắp hết hạn hỗ trợ ({eol})",
                           f"Còn {(eol - today).days} ngày là {label} {key} ngừng nhận bản vá.", "Lên kế hoạch nâng phiên bản.", "low", "devops", 0.8))
    return out


def _runtime_findings(cf, no, text, kinds, today) -> list[Finding]:
    """Phiên bản runtime khai báo trong CI / file version / manifest."""
    out = []
    checks: list[tuple[str, dict, str]] = []
    base = cf.path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if "runtime_file" in kinds:
        s = text.strip()
        if base in (".python-version", "runtime.txt") and (vm := re.search(r"(?:python-)?(\d+\.\d+)", s)):
            checks.append(("Python", PYTHON_EOL, vm.group(1)))
        elif base in (".nvmrc", ".node-version") and (vm := re.search(r"v?(\d+)", s)):
            checks.append(("Node.js", NODE_EOL, vm.group(1)))
        elif base == ".go-version" and (vm := re.search(r"(\d+\.\d+)", s)):
            checks.append(("Go", GO_EOL, vm.group(1)))
        elif base == ".ruby-version" and (vm := re.search(r"(\d+\.\d+)", s)):
            checks.append(("Ruby", RUBY_EOL, vm.group(1)))
        elif base == ".tool-versions":
            for tool, table, rx in (("python", PYTHON_EOL, r"(\d+\.\d+)"), ("nodejs", NODE_EOL, r"(\d+)"), ("golang", GO_EOL, r"(\d+\.\d+)"), ("ruby", RUBY_EOL, r"(\d+\.\d+)")):
                if s.startswith(tool + " ") and (vm := re.search(rx, s[len(tool):])):
                    checks.append((tool.capitalize(), table, vm.group(1)))
    elif "yaml" in kinds or "ci" in kinds:
        tail = text.split(":", 1)[1] if ":" in text else text
        if PY_VER_KEY_RE.search(text):
            checks += [("Python", PYTHON_EOL, v) for v in re.findall(r"\b(2\.7|3\.\d+)\b", tail)]
        elif NODE_VER_KEY_RE.search(text):
            checks += [("Node.js", NODE_EOL, v) for v in re.findall(r"\b(\d{1,2})(?:\.\d+)*\b", tail)]
        elif GO_VER_KEY_RE.search(text):
            checks += [("Go", GO_EOL, v) for v in re.findall(r"\b(1\.\d+)", tail)]
        elif (m := RUNS_ON_RE.search(text)):
            label = m.group(1)
            if label in RUNNER_REMOVED and _d(RUNNER_REMOVED[label]) <= today:
                out.append(_mk(cf, no, text, "EOL-RUNNER", f"Runner `{label}` đã bị GitHub gỡ từ {RUNNER_REMOVED[label]}",
                               "Job khai báo nhãn này sẽ chờ mãi hoặc thất bại vì không còn runner nhận.",
                               f"runs-on: {label.split('-')[0]}-latest", "high", "devops", 0.95))
    elif "deps_py" in kinds and (m := DJANGO_PIN_RE.match(text)):
        checks.append(("Django", DJANGO_EOL, m.group(1)))
    for label, table, key in checks:
        st, eol = eol_status(table, key, today)
        if st == "expired":
            newest = latest_supported(table, today)
            out.append(_mk(cf, no, text, "EOL-RUNTIME", f"{label} {key} đã hết hạn hỗ trợ từ {eol}",
                           f"{label} {key} không còn bản vá bảo mật từ {eol}; code chạy/kiểm thử trên phiên bản này mang lỗ hổng đã biết.",
                           f"Nâng lên {label} {newest}" if newest else "Nâng lên phiên bản còn được hỗ trợ.", "medium", "devops", 0.85))
        elif st == "soon":
            out.append(_mk(cf, no, text, "EOL-RUNTIME", f"{label} {key} sắp hết hạn hỗ trợ ({eol})",
                           f"Còn {(eol - today).days} ngày.", "Lên kế hoạch nâng phiên bản.", "low", "devops", 0.8))
    return out


def _dep_findings(cf, no, text, kinds) -> list[Finding]:
    hits: list[tuple[str, tuple]] = []
    if "deps_py" in kinds and (m := PY_REQ_RE.match(text)):
        name = m.group(1).lower().replace("_", "-")
        if name in PYPI_ABANDONED:
            hits.append((name, PYPI_ABANDONED[name]))
    elif "deps_js" in kinds and (m := NPM_DEP_RE.match(text)):
        name = m.group(1)
        if name in NPM_ABANDONED:
            hits.append((name, NPM_ABANDONED[name]))
    elif "deps_go" in kinds and (m := GO_REQ_RE.match(text)):
        mod = m.group(1)
        for key, meta in GO_ABANDONED.items():
            if mod == key or mod.startswith(key + "/"):
                hits.append((mod, meta))
    elif "deps_java" in kinds:
        for m in POM_ART_RE.finditer(text):
            if m.group(1) in JAVA_ABANDONED:
                hits.append((m.group(1), JAVA_ABANDONED[m.group(1)]))
        for m in GRADLE_DEP_RE.finditer(text):
            ga = f"{m.group(1)}:{m.group(2)}"
            if ga in JAVA_ABANDONED:
                hits.append((ga, JAVA_ABANDONED[ga]))
    out = []
    for name, (title, fix, sev, cat, conf) in hits:
        out.append(_mk(cf, no, text, "DEP-ABANDONED", title, f"`{name}` không còn được bảo trì hoặc đã bị thay thế; giữ nó là nợ kỹ thuật và rủi ro bảo mật.",
                       f"Thay bằng: {fix}" if fix else "", sev, cat, conf))
    return out


def _past_date_compare(cf, no, text, today) -> list[Finding]:
    if not COMPARE_RE.search(text.replace("=>", "").replace("->", "")):
        return []
    found: date | None = None
    for rx, mode in DATE_CTOR_RES:
        m = rx.search(text)
        if not m:
            continue
        y, mo, d = m.group(1), m.group(2), m.group(3)
        try:
            month = GO_MONTHS.get(mo, 0) if mode == 2 else int(mo) + (1 if mode == 1 else 0)
            found = date(int(y), month, int(d))
        except ValueError:
            continue
        break
    if found is None:
        m = re.search(r"[\"'](20\d{2}-\d{2}-\d{2})(?:T[^\"']*)?[\"']", text)
        if m:
            try:
                found = date.fromisoformat(m.group(1))
            except ValueError:
                found = None
    if found is None or found >= today:
        return []
    return [_mk(cf, no, text, "DATE-PAST", f"So sánh với mốc thời gian đã qua ({found}): một nhánh không bao giờ chạy nữa",
                f"Mốc {found} đã qua {(today - found).days} ngày, nên điều kiện này giờ luôn cho cùng một kết quả: nhánh còn lại là code chết, "
                "và nếu đây là cờ chuyển đổi tạm thời thì đã đến lúc dọn.",
                "Bỏ điều kiện và nhánh chết; nếu vẫn cần, đưa mốc vào cấu hình kèm ticket dọn dẹp.", "medium", "maintainability", 0.65)]


def _rules_for(kinds: set[str]) -> list[Rule]:
    rules: list[Rule] = []
    if "python" in kinds:
        rules += PYTHON_RULES
    if "js" in kinds:
        rules += JS_RULES
    if "go" in kinds:
        rules += GO_RULES
    if "java" in kinds:
        rules += JAVA_RULES
    if "docker" in kinds:
        rules += DOCKER_RULES
    if "ci" in kinds:
        rules += CI_RULES
    if kinds & {"shell", "ci", "docker", "yaml"}:
        rules += [r for r in PYTHON_RULES if r.id == "PY-SETUP-INSTALL"]
    return rules


_PY_ASYNC_RE = re.compile(r"^\s*async\s+def\b")
_PY_DEF_RE = re.compile(r"^\s*(?:def|class)\b")
_PY_LOOP_RE = re.compile(r"^\s*(?:for|while)\b")


def _code_checks(cf: ChangedFile, lines: list[tuple[int, str]], selected: set[int] | None, kinds: set[str], today: date) -> list[Finding]:
    """selected=None -> mọi dòng; nếu không chỉ các số dòng trong selected (dòng '+' của diff)."""
    out: list[Finding] = []
    rules = _rules_for(kinds)
    stack: list[tuple[int, str]] = []       # (indent, 'async' | 'def' | 'loop') — chỉ Python
    is_py = "python" in kinds
    for no, text in lines:
        stripped = text.strip()
        code_line = bool(stripped) and not stripped.startswith("#")
        if is_py and code_line:
            indent = len(text) - len(text.lstrip())
            while stack and stack[-1][0] >= indent:
                stack.pop()
        pick = selected is None or no in selected
        if pick and stripped and not _is_comment(text, kinds):
            in_loop = any(k == "loop" for _, k in stack)
            fn = next((k for _, k in reversed(stack) if k in ("async", "def")), "")
            for r in rules:
                if r.ctx == "loop" and not in_loop:
                    continue
                if r.ctx == "async" and fn != "async":
                    continue
                m = r.rx().search(text)
                if not m:
                    continue
                g = next((x for x in m.groups() if x), "") if m.groups() else ""
                out.append(_mk(cf, no, text, r.id, r.title.format(g), r.explanation, r.fix.format(g) if "{0}" in r.fix else r.fix,
                               r.severity, r.category, r.confidence))
            if kinds & {"docker", "yaml"}:
                out += _image_findings(cf, no, text, today)
            if kinds & {"yaml", "ci", "runtime_file", "deps_py"}:
                out += _runtime_findings(cf, no, text, kinds, today)
            if kinds & {"deps_py", "deps_js", "deps_go", "deps_java"}:
                out += _dep_findings(cf, no, text, kinds)
            if kinds & {"python", "js", "go", "java"}:
                out += _past_date_compare(cf, no, text, today)
        if is_py and code_line:
            indent = len(text) - len(text.lstrip())
            if _PY_ASYNC_RE.match(text):
                stack.append((indent, "async"))
            elif _PY_DEF_RE.match(text):
                stack.append((indent, "def"))
            elif _PY_LOOP_RE.match(text):
                stack.append((indent, "loop"))
    return out


# ---------------------------------------------------------------- điểm vào

def scan(files: list[ChangedFile], repo_dir: str, *, today: date | None = None, scope: str | None = None,
         warn_days: int | None = None) -> list[Finding]:
    """Quét các file thay đổi. scope: 'diff' = code lỗi thời chỉ ở dòng '+' (mặc định), 'file' = cả file.
    Khoá/chứng chỉ hết hạn luôn quét cả file (trừ file test)."""
    today = today or date.today()
    scope = scope or settings.stale_scope
    warn = settings.stale_warn_days if warn_days is None else warn_days
    out: list[Finding] = []
    for cf in files:
        if cf.is_binary or cf.is_deleted or is_generated_path(cf.path) or _is_doc(cf.path):
            continue
        lines, _from_disk = _read_lines(repo_dir, cf)
        if not lines:
            continue
        kinds = _kinds(cf.path)
        found: list[Finding] = []
        if not is_test_path(cf.path):
            found += _credential_checks(cf, lines, "\n".join(t for _, t in lines), today, warn)
        selected = None if scope == "file" else set(cf.added_only)
        found += _code_checks(cf, lines, selected, kinds, today)
        if len(found) > MAX_PER_FILE:
            found.sort(key=lambda f: (-SEVERITY_RANK[f.severity], f.line))
            found = found[:MAX_PER_FILE]
        out += found
    log.info("stale: %d phát hiện trong %d file (khoá hết hạn / code lỗi thời)", len(out), len(files))
    return out
