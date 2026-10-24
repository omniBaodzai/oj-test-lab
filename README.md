# OJ Test Lab

Công cụ **độc lập** để sinh test case, kiểm tra tính hợp lệ và đánh giá chất lượng bộ test cho các bài toán Online Judge (dự kiến tích hợp với UTH Online Judge ở giai đoạn sau). Dự án không phụ thuộc, không đọc và không ghi vào ứng dụng UTH Online Judge hay cơ sở dữ liệu của nó.

## Mục đích

1. Sinh test đầu vào **xác định (deterministic)** theo seed.
2. Kiểm tra chặt (strict) mọi input trước khi xuất.
3. Sinh output mong đợi bằng *reference solution* đáng tin cậy.
4. Chạy các lời giải sai **mô phỏng** để xem bộ test có phát hiện được không.
5. Xuất file `.in`/`.out`, `manifest.json`, `quality-report.json` và `tests.zip`.

## Kiến trúc

```
oj_test_lab/
  models.py        # Problem, TestCase, Candidate, CompareMode (giao diện lõi)
  comparator.py    # So sánh output: EXACT / TRIM
  evaluator.py     # Chạy Candidate trên bộ test, ghi nhận test phát hiện lỗi
  suite.py         # Dựng bộ test, báo cáo chất lượng, manifest, ZIP, nạp thư mục
  paths.py         # Tên file an toàn, chống path traversal
  cli.py           # Lệnh CLI (argparse)
  problems/
    __init__.py    # REGISTRY các bài toán
    data_types_io/ # Bài mẫu: spec, generator, validator, reference, wrong_solutions
tests/             # pytest
```

Engine chỉ làm việc với đối tượng `Problem` (các hàm `generate`, `validate`, `reference`, danh sách lời giải sai), nên thêm bài mới **không cần sửa engine**.

## Cài đặt

Yêu cầu Python ≥ 3.12. Các lệnh sinh test/Docker không cần thư viện ngoài; lệnh `problem analyze` cần `openai` và `python-dotenv` (khai báo trong `pyproject.toml`: `pip install -e .`). Cần `pytest` để chạy test.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]      # hoặc: pip install pytest
```

## Sử dụng CLI

```powershell
# Sinh bộ test (mặc định 28 test, seed 42, thư mục output/data-types-io, có tests.zip)
python -m oj_test_lab generate --problem data-types-io --count 28 --seed 42
#   tuỳ chọn: --output-dir DIR, --no-zip

# Kiểm tra input hợp lệ, không trùng, .out khớp reference, checksum khớp manifest
python -m oj_test_lab validate --problem data-types-io --input-dir output/data-types-io

# Đánh giá lời giải sai mô phỏng, ghi quality-report.json
python -m oj_test_lab evaluate --problem data-types-io --input-dir output/data-types-io
#   tuỳ chọn: --no-write (không ghi báo cáo), --strict (exit 1 nếu có lời giải sai lọt lưới)

# Xuất lại (.out, manifest, báo cáo) từ các file .in; --zip để tạo tests.zip
python -m oj_test_lab export --problem data-types-io --input-dir output/data-types-io --zip
#   tuỳ chọn: --output-dir DIR (mặc định ghi đè cùng thư mục)
```

Mã thoát: `0` thành công, `1` lỗi (bài không tồn tại, input không hợp lệ, đường dẫn không an toàn, …), `2` sai cú pháp CLI. Console chỉ in số liệu tổng hợp, **không in nội dung test**.

## File `.in` / `.out`

```
output/data-types-io/
  01.in 01.out ... 28.in 28.out
  manifest.json        # problem, generator_version, seed, test_count, test_ids, category, tên file, sha256
  quality-report.json  # số test, hợp lệ, trùng, lời giải sai phát hiện/lọt lưới, cảnh báo
  tests.zip            # các file .in/.out + manifest.json (xác định, timestamp cố định)
```

Mọi file là UTF-8, xuống dòng `LF` (ghi dạng bytes nên giống nhau trên mọi hệ điều hành). `generate` chỉ xoá các file nó quản lý (`NN.in`, `NN.out`, `manifest.json`, `quality-report.json`, `tests.zip`) trong thư mục đích; file khác được giữ nguyên.

## Bài mẫu `data-types-io`

Input 5 dòng: `X` (|X| ≤ 10^9), `Y` (|Y| ≤ 10^18), `C` (một chữ cái ASCII), `F` (|F| ≤ 10^6), `D` (|D| ≤ 10^9). Output 5 dòng: `X`, `Y`, `C`, `F` đúng 2 chữ số thập phân, `D` đúng 9 chữ số thập phân.

**Định dạng input chặt:** đúng 5 dòng, mỗi dòng kết thúc bằng một `\n`, không khoảng trắng đầu/cuối, không dòng thừa, không `\r`, không số 0 đầu, không `-0`.

**Chính sách số thực (F, D):**

- Là số thập phân thuần (không số mũ, không `+`, NaN, Inf, không âm-không `-0.00`), F tối đa 2 và D tối đa 9 chữ số thập phân.
- Reference dùng **số thập phân chính xác** (`decimal.Decimal`, không dùng float nhị phân) và chỉ đệm thêm số 0. Vì input không có nhiều chữ số thập phân hơn output nên **không xảy ra làm tròn** (nếu cần: `ROUND_HALF_EVEN`).
- Để kết quả của C++ `float` (F) / `double` (D) trùng khớp tuyệt đối, validator yêu cầu giá trị **biểu diễn chính xác** trong binary32 / binary64 (F là bội của 1/4; D là bội của 1/512 trong các test sinh ra). Các số như `0.1` **cố ý không được sinh**.
- Không khẳng định định dạng `Decimal` của Python tương đương `printf`/`iostream` của C++ với số bất kỳ.

**Sinh test (mặc định 28):** 24 test bắt buộc (normal, zero, negative, integer-boundary, character, float-format) + test `random` theo seed. Luôn có Y ∈ {2147483647, 2147483648, -2147483648, -2147483649, 10^18, -10^18} và Y > 32-bit. `--count` tối thiểu là 24 để giữ độ phủ biên bắt buộc. Không sinh test sai ràng buộc, không trùng input.

## Reference solution

`problems/data_types_io/reference.py` kiểm tra input bằng validator, đọc `X`, `Y` bằng `int` của Python (không mất độ chính xác), định dạng F/D bằng thập phân chính xác và trả về đúng 5 dòng, kết thúc bằng `\n`, mã hoá UTF-8.

## Đánh giá lời giải sai (wrong-solution testing)

Các lời giải sai là **hàm Python tin cậy, dựng sẵn, mô phỏng lỗi** (đánh dấu `kind: simulated-python`, `simulated: true` trong báo cáo):

| ID | Lỗi mô phỏng |
|---|---|
| `wrong-y-int32-overflow` | Y bị cắt về số nguyên 32-bit có dấu |
| `wrong-f-decimals` | In F với 3 chữ số thập phân |
| `wrong-d-decimals` | In D với 6 chữ số thập phân |
| `wrong-negative-sign` | Mất dấu âm của X và F (abs) |

Mỗi lời giải chạy trên mọi test; test có output khác reference (theo comparator) được ghi vào `detected_by_tests`. Lời giải không bị test nào phát hiện nằm trong `escaped_incorrect_solutions`. **Đây không phải bằng chứng phát hiện lỗi `int` thật của C++** — chưa có chương trình C++ nào được biên dịch hay chạy.

Giao diện `Candidate(id, description, run, kind)` được thiết kế để giai đoạn sau thêm chương trình biên dịch thật (qua sandbox runner).

## So sánh output

- **EXACT** (mặc định): chuẩn hoá `CRLF`/`CR` → `LF`, sau đó phải trùng từng ký tự; thiếu/thừa `\n` cuối, dấu cách cuối dòng đều là khác.
- **TRIM**: chuẩn hoá newline, bỏ dấu cách/tab cuối mỗi dòng và các dòng trống đầu/cuối văn bản.
- Cả hai chế độ **không bao giờ** gộp khoảng trắng bên trong (`1 2` ≠ `1  2`).

## Thêm bài mới

1. Tạo `oj_test_lab/problems/<ten_bai>/` với: generator (`generate(count, seed) -> [(category, input_text)]`), validator (`validate(text) -> [lỗi]`), reference (`solve(text) -> output`), danh sách `Candidate` sai.
2. Trong `__init__.py` của gói đó tạo `PROBLEM = Problem(id="ten-bai", ...)` (id chỉ gồm `a-z0-9-`).
3. Đăng ký vào `REGISTRY` trong `oj_test_lab/problems/__init__.py`.
4. Viết test trong `tests/`. Không cần sửa engine/CLI.

## Chạy test

```powershell
python -m pytest -q
```

## Chạy C++ thật trong Docker (`evaluate-cpp`)

> **Phạm vi:** chỉ dành cho các lời giải C++ *đáng tin cậy, do dự án cung cấp* (demo cục bộ). Docker **không** phải ranh giới bảo mật mạnh trước mã độc; không mở runner này thành dịch vụ chạy mã công khai và không dùng nó cho bài nộp không tin cậy. CLI không nhận file mã tuỳ ý, chỉ nhận ID ứng viên đã đăng ký.

Yêu cầu: Docker đang chạy và image đã pin (kéo một lần):

```powershell
docker pull gcc:13.3.0
```

Image dùng trong code: `gcc:13.3.0@sha256:1d71f0f3450214bef38fe09e6f610fb6cca90cf97b43f4ce845bfc32a4168818` (g++ 13.3.0, `-std=c++17 -O2 -pipe`).

```powershell
python -m oj_test_lab evaluate-cpp --problem data-types-io --input-dir output/data-types-io
python -m oj_test_lab evaluate-cpp --problem data-types-io --input-dir output/data-types-io --candidate cpp-wrong-int-y --time-limit 5
```

Ứng viên của `data-types-io`: `cpp-correct-long-long` (Y là `long long`) và `cpp-wrong-int-y` (Y là `int`), mã nguồn ở `oj_test_lab/problems/data_types_io/cpp/`. Kết quả ghi vào `cpp-execution-report.json` (`execution: "real"`, `simulated: false`), **không** ghi đè `quality-report.json` mô phỏng.

Thiết kế: mỗi ứng viên được biên dịch **một lần** trong container tạm, sau đó mỗi test chạy trong một container tạm riêng (stdin = `.in`, stdout/stderr tách riêng, so sánh stdout với `.out` bằng comparator của bài, mặc định EXACT). Trạng thái: AC, WA, RE, TLE, CE; lỗi hạ tầng Docker được báo riêng (`INFRA`, exit code 1) và không bao giờ bị tính là WA.

Cấu hình container: `--network none`, `--user 65534:65534`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, `/tmp` là tmpfs nhỏ (`noexec` khi chạy), `--memory`, `--memory-swap`, `--pids-limit`, `--cpus 1`, `ulimit fsize/core`, timeout phía host, `--rm` và `docker rm -f` khi timeout; không `--privileged`, không mount docker socket, không publish cổng. Thư mục tạm chứa mã nguồn được mount chỉ đọc, thư mục build chỉ ghi được khi biên dịch và chỉ đọc khi chạy.

## Quản lý đặc tả bài toán (`problem`)

Nền tảng lưu và kiểm tra cấu trúc dữ liệu cho quy trình AI. Đặc tả vẫn là dữ liệu người dùng viết; việc hiểu ngữ nghĩa và biến đề bài tự nhiên thành bộ test cần AI, review và sandbox ở các bước sau.

`ProblemSpec` (đặc tả do người dùng viết, thuần dữ liệu) **khác** với `Problem` (bài toán thực thi được của engine, đăng ký trong `REGISTRY`). Đặc tả không làm thay đổi `REGISTRY`.

```powershell
python -m oj_test_lab problem create   --file examples/sum-two-integers.json   # thêm --force để ghi đè
python -m oj_test_lab problem list
python -m oj_test_lab problem show     --id sum-two-integers
python -m oj_test_lab problem validate --id sum-two-integers      # hoặc --file <json>
# Tuỳ chọn chung: python -m oj_test_lab problem --workspace DIR <lệnh> ...  (mặc định ./workspace)
```

Mỗi đặc tả là một file `workspace/problems/<id>.json` (UTF-8, ghi nguyên tử qua file tạm + `os.replace`/`os.link`; không ghi đè nếu chưa có `--force`; `id` chỉ gồm `a-z0-9-`, tối đa 64 ký tự, nên không thể path traversal; id trùng với bài dựng sẵn như `data-types-io` bị từ chối).

### Lược đồ JSON (`schema_version` = 1)

| Trường | Kiểu | Bắt buộc | Quy tắc |
|---|---|---|---|
| `schema_version` | int | không (mặc định 1) | chỉ hỗ trợ 1 |
| `id` | string | có | `[a-z0-9][a-z0-9-]{0,63}`, không kết thúc bằng `-`, không có `--`, không phải tên dành riêng của Windows (`con`, `nul`, `com1`…) |
| `title` | string | có | không rỗng, ≤ 200 ký tự |
| `statement` | string | có | không rỗng, ≤ 20 000 ký tự |
| `input_description`, `output_description` | string | có | không rỗng, ≤ 5 000 ký tự |
| `constraints` | list[string] | có | 1–50 mục, mỗi mục không rỗng, ≤ 1 000 ký tự |
| `samples` | list[{`input`,`output`}] | không | ≤ 20 mục; mỗi mục có đúng 2 khóa, là chuỗi không rỗng ≤ 5 000 ký tự |
| `desired_test_count` | int | có | 1–10 000 (không nhận bool/float/chuỗi) |
| `created_at`, `updated_at` | string ISO-8601 | không khi nhập | công cụ tự điền; file lưu luôn có |

Trường lạ, khóa JSON trùng lặp, ký tự NUL, file không phải UTF-8 hoặc > 1 MB đều bị từ chối; mọi lỗi được liệt kê cùng lúc. Ví dụ: [examples/sum-two-integers.json](examples/sum-two-integers.json).

**Kiểm tra cấu trúc ≠ kiểm tra ngữ nghĩa.** Công cụ không hiểu đề bài hay ràng buộc (ví dụ không biết `-10^9 <= a, b <= 10^9` có hợp lý, có khớp mẫu hay không). Việc đó do con người xem xét.

## Phân tích bằng AI qua OpenRouter (`problem analyze`, `proposal`)

Giai đoạn này tạo **đề xuất (proposal)** có cấu trúc từ một đặc tả đã lưu. Đề xuất phân tích không được thực thi; bước sinh mã và duyệt mã nằm ở quy trình code proposal bên dưới.

### Cấu hình (`.env` hoặc biến môi trường; chỉ đọc các khóa `OPENROUTER_*`)

Sao chép `.env.example` thành `.env` (đã bị Git bỏ qua). Biến môi trường ghi đè `.env`. Khóa API không bao giờ nhận qua dòng lệnh và không được in ra.

| Biến | Ý nghĩa |
|---|---|
| `OPENROUTER_API_KEY` | Bắt buộc (trừ `--dry-run`) |
| `OPENROUTER_MODEL` | Bắt buộc (hoặc `--model`); không có mặc định vì gọi API tốn phí |
| `OPENROUTER_BASE_URL` | Mặc định `https://openrouter.ai/api/v1` (phải https, trừ localhost) |
| `OPENROUTER_TIMEOUT_SECONDS` | Mặc định 120 |
| `OPENROUTER_MAX_OUTPUT_TOKENS` | Mặc định 4000 |
| `OPENROUTER_MAX_RETRIES` | Mặc định 2 (backoff mũ 1s, 2s, ...; không retry lỗi xác thực/thanh toán) |

### Lệnh

```
python -m oj_test_lab problem analyze --id sum-two-integers [--model M] [--max-tokens N] [--dry-run]
python -m oj_test_lab proposal list [--spec-id ID]
python -m oj_test_lab proposal show --id PROPOSAL_ID
```

`--dry-run` không gọi mạng. **Lưu ý riêng tư:** `analyze` gửi nội dung đặc tả (tiêu đề, đề bài, mô tả, ràng buộc, mẫu) tới bên thứ ba (OpenRouter và nhà cung cấp model).

### Kiến trúc và lưu trữ

`ai/provider.py` (giao thức `AIProvider`) → `ai/openrouter.py` (`OpenRouterProvider`) → `ai/analyzer.py` (`ProblemAnalyzer`, độc lập nhà cung cấp) → `ai/proposal_store.py` (`workspace/proposals/<id>.json`, ghi nguyên tử, không ghi đè). Thêm Gemini = thêm một lớp provider mới. Đầu ra AI được xem là dữ liệu không tin cậy: kiểm tra schema nghiêm ngặt, từ chối trường lạ (kể cả `approved`), metadata nguồn gốc (provider, model, thời gian, `spec_sha256`, phiên bản prompt/schema) do code của ta ghi. Mọi proposal có `approved=false`. `proposal show` cảnh báo nếu đặc tả đã thay đổi sau khi phân tích.

### Giới hạn phân tích

Nội dung đề xuất là ý kiến chưa kiểm chứng của AI, không phải chứng minh đúng và phụ thuộc chất lượng model.

### Chuẩn bị cho AI

`oj_test_lab/specs/ai_interface.py` định nghĩa hợp đồng `SpecAnalyzer`/`GenerationPlanProposal`; bản cài đặt thật nằm ở `oj_test_lab/ai/`. Các loại dữ liệu được tách thư mục: `workspace/problems/`, `workspace/proposals/`, `workspace/code-proposals/` và `workspace/test-suites/`.

## Đề xuất mã bằng AI và duyệt thủ công (`code-proposal`)

AI viết **generator (Python)** và **reference solution (C++17)** dưới dạng *đề xuất không tin cậy* để người xem xét. Trước khi duyệt, mã không được biên dịch, import, `eval`/`exec` hay chạy; sau khi duyệt, chỉ `UntrustedSandbox` mới được phép thực thi khi operator opt-in.

```
python -m oj_test_lab code-proposal generate --proposal-id PLAN_ID [--model M] [--max-tokens N] [--dry-run]
python -m oj_test_lab code-proposal list [--spec-id ID] [--proposal-id PLAN_ID]
python -m oj_test_lab code-proposal show --id CODE_ID [--no-code]
python -m oj_test_lab code-proposal approve --id CODE_ID --reviewer "Tên" [--confirm-id CODE_ID]
python -m oj_test_lab code-proposal export --id CODE_ID [--out-dir DIR]
```

- **Điều kiện:** cần proposal phân tích (`problem analyze`) còn khớp đặc tả; cấu hình `OPENROUTER_*` như trên. Lưu tại `workspace/code-proposals/<id>.json` (UTF-8, ghi nguyên tử, không ghi đè). Mặc định ít nhất 8000 token đầu ra.
- **Nội dung:** `generator_code` (`def generate(seed, count) -> list[str]`), `reference_code`, giải thích thuật toán, độ phức tạp, chiến lược test, `numeric_range_analysis` (miền giá trị mọi đại lượng, ví dụ tổng hai số trong `[-1e9, 1e9]` nằm trong `[-2e9, 2e9]` nên vượt `int32`), edge case, test đối kháng, giả định, rủi ro, câu hỏi chưa giải quyết (`blocking` hoặc không). Nguồn gốc (provider, model, thời gian, `spec_sha256`, `plan_sha256`, `content_sha256`) do code của ta ghi, AI không được cung cấp.
- **Phát hiện lỗi thời (stale):** nếu đặc tả hoặc plan proposal đổi/mất sau khi sinh, `show`/`list` cảnh báo và `approve` bị từ chối.
- **Duyệt:** `approve` yêu cầu xác nhận rõ ràng (gõ lại đúng id khi chạy tương tác, hoặc `--confirm-id`; không có xác nhận thì từ chối), từ chối nếu stale hoặc còn câu hỏi `blocking`, ghi `reviewer`, thời gian và hash nội dung đã duyệt; sửa tay file sau đó sẽ bị phát hiện. Duyệt chỉ ghi quyết định, **không chạy gì**.
- **`export`:** ghi `generator.py.txt`, `reference.cpp.txt`, `REVIEW.txt` (đuôi `.txt` có chủ đích) để đọc bằng tay.
- **Cảnh báo tĩnh** (regex) chỉ mang tính tham khảo; không có cảnh báo KHÔNG có nghĩa là an toàn. Kiểm tra tĩnh và Docker đều **không** đủ làm sandbox cho mã không tin cậy; việc chạy mã đã duyệt thuộc giai đoạn sau và cần sandbox riêng.

## Web UI (TASK 6)

Ứng dụng FastAPI + HTML/CSS/JavaScript thuần nằm trong `oj_test_lab/web/`. Ứng dụng lưu dữ liệu vào `workspace/` mặc định, không đọc API key ở trình duyệt và chỉ chấp nhận request từ localhost kèm token nội bộ của giao diện.

```powershell
# cài dependency (nếu chưa chạy)
pip install -e .
# chạy tại localhost; mở http://127.0.0.1:8000
uvicorn oj_test_lab.web.app:app --host 127.0.0.1 --port 8000
```

Giao diện hỗ trợ lưu đặc tả, phân tích AI, sinh code proposal, xem code, duyệt thủ công, sinh test, xem báo cáo và tải `tests.zip`. Các endpoint JSON tương ứng là `/api/problems`, `/api/problems/{id}/analyze`, `/api/problems/{id}/code`, `/api/code/{id}/approve`, `/api/generate`, `/api/suites/{id}` và `/api/suites/{id}/download`.

Sinh test từ code AI bị tắt mặc định. Chỉ bật khi đã hiểu rõ rủi ro và Docker Desktop đang chạy:

```powershell
$env:OJ_TEST_LAB_ENABLE_UNTRUSTED_EXEC="1"
uvicorn oj_test_lab.web.app:app --host 127.0.0.1 --port 8000
```

`UntrustedSandbox` chỉ chạy image đã có sẵn, không tự pull, không có network/credentials/project mount/Docker socket, chạy non-root với giới hạn tài nguyên và kiểm tra Docker Desktop VM hoặc runtime gVisor/Kata. Docker vẫn không phải ranh giới bảo mật mạnh trước kernel/runtime exploit; nếu không đạt điều kiện, endpoint từ chối chạy. Báo cáo ghi rõ đây là kiểm tra độc lập bằng generator/reference trong sandbox, không phải chứng minh thuật toán đúng.

### Validate Code và Repair with AI (TASK 7)

Sau khi sinh code, chọn **Validate code** để chạy AST/static checks và, khi sandbox sẵn sàng, dynamic checks ở các count `1, 3, 20`, nhiều seed và nhiều process riêng. Validator kiểm tra interface, syntax, số lượng/format/uniqueness input, constraint máy hiểu được, C++17 compile, output formatting, sample output và determinism. Constraint tự nhiên không nhận diện được sẽ được ghi là `warning/skipped`, không bị trình bày như đã xác minh.

Nếu validation thất bại, **Repair with AI** là thao tác explicit. Diagnostics được giới hạn và làm sạch trước khi gửi OpenRouter; tối đa 2 lần repair. Nếu model trả nguyên generator/reference đang lỗi, hệ thống báo rõ `RepairUnchangedError` và không coi repair là thành công. Mỗi bản sửa hợp lệ được lưu thành proposal mới trong `workspace/code-proposals/`, có `parent_code_proposal_id`, `repair_attempt`, fingerprint và vẫn `approved=false`. Approval chỉ được phép khi validation report pass và fingerprint report khớp chính xác nội dung hiện tại. Không có static check, compile, sample hay sandbox run nào là bằng chứng code an toàn hoặc đúng toán học.

### Open Questions và approval (TASK 9)

Các câu hỏi `blocking` của proposal active được hiển thị cùng nguồn proposal, problem và parent lineage. Reviewer có thể nhập câu trả lời và xác nhận từng câu; resolution được ghi audit riêng với reviewer, timestamp, spec/plan/content fingerprint. Khi spec hoặc plan thay đổi, resolution cũ trở thành stale và không thể dùng để approval. Approval chỉ tiếp tục khi mọi blocking question đã được resolve trên binding hiện tại và validation report vẫn pass với fingerprint khớp.

### Chỉnh sửa thủ công (TASK 8)

Nút **Edit code** mở bản sao của proposal đang chọn trong hai textarea. **Save as New Proposal** chỉ tạo proposal mới với `provider: manual`, parent ID và fingerprint riêng; proposal gốc cùng lịch sử validation không bị ghi đè. Source rỗng, có control character hoặc vượt giới hạn schema bị từ chối trước khi lưu. Lưu thủ công không gọi OpenRouter, không import/compile/execute code; proposal mới vẫn phải qua **Validate code** trước khi approval.

## Giới hạn hiện tại

- Chỉ có 1 bài mẫu; lời giải sai chỉ là mô phỏng bằng Python.
- Số thực giới hạn ở giá trị biểu diễn chính xác nhị phân; chưa xác minh tương đương định dạng với C++ trên số thực tuỳ ý.
- Chạy code AI chỉ khả dụng khi operator bật opt-in và Docker đạt kiểm tra isolation; không có fallback chạy trên host.
- Phát hiện trùng lặp chung dựa trên văn bản input; generator của bài có thể trùng theo ngữ nghĩa chặt hơn.

## Kế hoạch tích hợp UTH Online Judge (tương lai)

- Sandbox runner (giới hạn thời gian/bộ nhớ, không mạng) để biên dịch và chạy lời giải C++ thật.
- Định dạng gói tests.zip + manifest tương thích với import của UTH Online Judge, dưới dạng bước import **thủ công/có xác nhận**; công cụ này không tự upload hay sửa CSDL.
- Thêm nhiều bài và generator, checker đặc biệt cho số thực.
