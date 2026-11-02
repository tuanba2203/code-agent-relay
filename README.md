# Codex Relay

**Dashboard chạy trên máy cá nhân để quản lý nhiều tài khoản Codex, theo dõi quota và bàn giao công việc giữa các tài khoản.**

Relay giúp bạn biết account nào đang được sử dụng, quota còn lại và thread nào thuộc cùng một công việc. Khi cần chuyển account, tool có thể compact cuộc hội thoại, xuất bản bàn giao Markdown và nạp context vào thread tương ứng của account đích.

Dự án độc lập, không phải sản phẩm chính thức của OpenAI. Không cần hosting riêng; tài khoản và dữ liệu quản lý nằm trên máy của bạn. Các thao tác Codex vẫn cần quyền sử dụng hợp lệ và tiêu thụ quota theo tài khoản.

> Đang phát triển, ưu tiên macOS. Chuyển account yêu cầu backend client dừng; desktop có tùy chọn tự đóng/mở lại. Hot switching, tự restart extension và tự chọn thread trong Codex desktop/VS Code chưa được hỗ trợ.

## Mục đích

- Theo dõi quota Codex của nhiều tài khoản trong một dashboard.
- Quản lý profile đăng nhập, alias và backup tài khoản.
- Chọn account đích dựa trên quota còn lại và thời điểm reset.
- Giữ một workflow chung xuyên nhiều account, tránh tạo thread trùng mỗi lần quay lại.
- Lưu journal, handoff và lịch sử để người dùng kiểm soát và debug.

Relay không cộng quota thành một pool và không tăng giới hạn của nhà cung cấp. Quota ChatGPT không nằm trong dashboard.

## Smart proxy + Relay (nhánh thử nghiệm)

Có chế độ proxy subscription opt-in: `python3 dashboard.py --port 8766 --proxy-port 8767`. Giữ account theo thread, forward request/stream, theo dõi quota và tạo handoff trước khi chuyển tuyến. Không sửa file auth global hay restart client theo account. Có panel quản lý thread, alias và checkpoint trong dashboard.

Chưa kiểm thử với desktop/VS Code thật; không bật mặc định, không tự đổi cấu hình client hoặc tự gọi model để checkpoint. Context được lưu riêng trên máy để phục hồi; lỗi gián đoạn không tự replay. Xem [docs/smart-proxy.md](docs/smart-proxy.md) để cấu hình, giới hạn và bài thử A → B.

## Tính năng

| Nhóm | Chức năng |
| --- | --- |
| Account | Thêm đăng nhập, sửa alias, xóa profile local, import/export mã hóa |
| Quota | Quota ngắn hạn/tuần nếu được báo cáo, thời điểm reset, refresh và countdown |
| Rotation | Thủ công hoặc tự động theo ngưỡng, chọn đích theo quota/reset |
| Context | Compact, xuất Markdown, checksum, import vào thread mới hoặc đã liên kết |
| Workflow | Mapping account–thread, alias chung, lịch sử và đồng bộ tên |
| Recovery | Journal bền vững, bản sao phiên nguồn, xác nhận sau lỗi |
| Giao diện | Log lưu local qua restart, light/dark theme |

## Yêu cầu

- macOS là môi trường triển khai hiện tại; chưa xác nhận đầy đủ Windows/Linux.
- Python 3. Backend hiện dùng standard library, không cần cài package Python ngoài.
- Codex CLI chính thức có `app-server` tương thích các RPC của Relay.
- Đăng nhập ChatGPT theo chế độ file; cơ chế này không hỗ trợ API key/service account.
- Các client dùng chung `CODEX_HOME` để quản lý cùng kho thread và đăng nhập.
- Trình duyệt hỗ trợ Web Crypto cho backup mã hóa.
- Node.js chỉ cần cho kiểm tra cú pháp JavaScript.

Relay tìm `codex` trong `PATH`, rồi thử đường dẫn đóng gói trong Codex.app/ChatGPT.app trên macOS. Tương thích phụ thuộc phiên bản Codex.

## Cài đặt và chạy local

Tải source, mở Terminal trong thư mục project:

```sh
python3 dashboard.py --port 8766
```

Mở **http://127.0.0.1:8766/** bằng Safari/Chrome riêng. Không dùng browser bên trong ứng dụng cần đóng khi rotation. Server bind `127.0.0.1`; nếu bỏ `--port`, cổng mặc định là `8765`.

Có thể chỉ định đường dẫn riêng:

```sh
python3 dashboard.py --port 8766 \
  --codex-home /absolute/path/to/codex-home \
  --accounts-dir /absolute/path/to/private-relay-accounts
```

### Kết nối account hiện tại

1. Trong `config.toml` của `CODEX_HOME` (mặc định `~/.codex`), đặt cấu hình sau ở cấp đầu file, trước các bảng TOML:

   ```toml
   cli_auth_credentials_store = "file"
   ```

2. Đăng nhập lại bằng Codex nếu trước đó dùng keychain/keyring.
3. Khởi động Relay. Server thử lưu profile đăng nhập hiện tại nếu đọc được.
4. Vào quản lý đăng nhập để thêm account hoặc sửa alias.

Xóa profile trong Relay không xóa tài khoản tại nhà cung cấp. Không đưa token vào issue hoặc log công khai.

### Alias và tên thread

Alias thủ công được ưu tiên. Khi chưa có alias, Relay dùng phần email trước `@` cộng loại gói từ metadata local:

| Ví dụ | Alias |
| --- | --- |
| `alice@example.com`, Plus | `alice_plus` |
| Business/Team | `alice_team` |
| Pro | `alice_pro` |
| Metadata thiếu loại gói | `alice_unknown` |

Tên thread: **`Tên workflow · Alias account`**, ví dụ `Website redesign · alice_team`. Các thread của nhiều account cùng xuất hiện trong Codex local vẫn phân biệt được. Đồng bộ tên sau khi đổi alias, khi client đã đóng.

## Cách hoạt động

### Đọc quota

Relay chạy Codex app-server qua stdio để đọc account và rate limits; đọc quota không tạo model turn. Dữ liệu chưa được báo cáo không được coi là 0 hoặc vô hạn.

| Trạng thái | Polling |
| --- | --- |
| Active, quota đổi trong 30 phút gần nhất, còn >50% | 5 phút |
| Active, quota đổi trong 30 phút gần nhất, còn 30–50% | 2 phút |
| Active, quota đổi trong 30 phút gần nhất, còn <30% | 30 giây |
| Active, quota không đổi hơn 30 phút | 30 phút |
| Account sẵn sàng hoặc disable | 30 phút |

Có refresh thủ công. Quota hiển thị phản ánh lần đọc gần nhất.

### Chọn account tiếp theo

Rotation tự động xét khi quota hiện tại chạm ngưỡng dự trữ cấu hình, thường khoảng 15% còn lại. Relay cân nhắc quota tuần, quota ngắn hạn, thời điểm reset và lượng quota đủ để tiếp tục. Ngưỡng nhận account đích phụ thuộc reserve/margin; account disable không được chọn.

Nếu các cửa sổ đang gây giới hạn đều reset trong khoảng 5 phút, Relay chờ và đọc lại thay vì chuyển ngay. Khởi động lại server tắt automatic rotation cho đến khi bật lại.

### Compact → export → switch → import

1. Chờ client Codex desktop, VS Code và Codex CLI đóng; không tự kill ứng dụng.
2. Kiểm tra source binding của tất cả workflow và quota account đích.
3. Sao lưu dữ liệu phiên local, compact từng thread và chờ xác nhận.
4. Tạo handoff Markdown, lưu checksum và journal.
5. Kiểm tra lại quota, đổi file đăng nhập và xác minh identity.
6. Dùng thread đã liên kết ở account đích; nếu chưa có thì tạo mới, đặt tên và lưu mapping.
7. Import toàn bộ handoff trước bước tiếp tục công việc.
8. Mở lại client và chọn thread theo tên/ID.

Handoff là tóm tắt có cấu trúc, không giữ nguyên từng chi tiết hội thoại. Khi tiếp tục cần đối chiếu file và trạng thái project thực tế.

### Workflow xuyên account

```text
Website redesign
  account A → thread 111
  account B → thread 112
  account C → thread 113
```

Quay lại B sẽ dùng thread 112. Hai workflow cùng project vẫn độc lập, không bị gộp chỉ vì chung thư mục.

### Continuation và recovery

Tùy chọn tiếp tục chạy một lượt công việc trong app-server riêng của Relay sau import. Lượt này có thể sửa file trong project được chọn, tắt mạng và không tự chấp thuận yêu cầu quyền. Nó không phải lượt chạy trong UI desktop/VS Code.

**Lần đầu nên tắt automatic và continuation.** Thử `Force compact & export` để tạo checkpoint trước khi thử rotation.

Khi lỗi, Relay giữ handoff/journal, không tự replay. Engine hiện có thể rollback về nguồn khi continuation lỗi dù import đã hoàn tất. Sửa đổi project do continuation tạo ra không được rollback.

- Đọc bước lỗi và danh sách import trước khi retry.
- Thiếu binding: dùng **Nạp handoff còn thiếu** để nhập bản đã lưu thay vì compact lại.
- **Xác nhận sau phục hồi** kiểm tra trạng thái local và gỡ rào cản job cũ, không chạy lại handoff. Nút bị khóa khi client còn chạy hoặc worker chưa dừng.
- **Force switch account** đổi đăng nhập trực tiếp, không compact/chuyển context, bỏ qua chọn đích theo quota; vẫn cần đóng client.

## Công nghệ và cấu trúc

| Thành phần | Công nghệ |
| --- | --- |
| Backend | Python standard library: HTTP server, threading, subprocess, SQLite, file locking |
| Frontend | HTML/CSS/JavaScript thuần, không cần framework build |
| Codex integration | app-server qua JSON-RPC/stdio |
| Backup qua UI | Web Crypto, AES-256-GCM, PBKDF2-SHA256 với 600.000 vòng |
| Lưu trữ | JSON registry/journal/log, Markdown handoff, ghi atomically |
| Test | unittest, fake RPC, fixture trong thư mục tạm |

```text
accounts.py          Profile, alias, file-auth switching
onboarding.py        Luồng đăng nhập
live_quota.py        app-server và quota
quota_schedule.py    Lịch polling
quota_policy.py      Chọn account/reset
rotation.py          Compact, handoff, import, recovery, log
managed_threads.py   Workflow/account/thread registry
dashboard.py         Server local và HTTP API
ui/                  Giao diện
build_standalone.py  Đóng gói HTML standalone
test_*.py            Bộ test
```

`Codex Relay.html` là giao diện standalone dùng dữ liệu browser-local, không thay thế server Python cho quota live, đăng nhập hoặc rotation.

## Dữ liệu và quyền riêng tư

Mặc định `.relay-accounts/` cạnh source lưu credential profile, backup đăng nhập, registry, journal, log, handoff, bản sao phiên và kết quả continuation.

File/thư mục riêng được tạo với quyền hạn chế. **Vault local không được mã hóa toàn bộ at rest.** File export qua UI được mã hóa bằng mật khẩu; dữ liệu nội bộ và CLI export không được coi là backup mã hóa tương đương.

Log không cố ý ghi token, raw RPC payload hoặc nội dung model. Tuy vậy email, alias, đường dẫn, ID thread và handoff vẫn có thể nhạy cảm. Không chia sẻ nguyên dữ liệu local để báo lỗi.

Chạy local không có nghĩa là offline: đăng nhập, đọc quota và model turn vẫn giao tiếp với dịch vụ Codex/OpenAI. Compact và tạo summary tiêu thụ quota. Relay không yêu cầu hosting riêng.

## Kiểm thử

```sh
python3 -m unittest -q
node --check ui/app.js
python3 build_standalone.py
```

Fake RPC test không thay thế kiểm tra tích hợp với phiên bản Codex thực tế.

## Public repo và đóng góp

Chỉ đưa source/tài liệu lên repo. `.gitignore` loại `.relay-accounts/`, `auth.json`, `*.auth.json`, Python cache và archive `*source.zip`. Kiểm tra file được track và nội dung archive trước khi chia sẻ; ignore không xóa dữ liệu đã commit.

Không commit credential, export account, journal/handoff thật, database phiên hoặc screenshot có dữ liệu riêng. Báo lỗi nên kèm phiên bản macOS/Codex/Python, bước tái hiện và log đã loại thông tin cá nhân.

Để người khác được sử dụng, sửa đổi và phân phối miễn phí rõ ràng, cần thêm **LICENSE** với giấy phép do chủ dự án chọn. README này không tự cấp giấy phép; chưa bổ sung LICENSE thay chủ dự án.

## Giới hạn hiện tại

- Chưa hot switch đăng nhập, restart riêng VS Code extension hay tự chọn thread trong client. Desktop hỗ trợ quit/reopen tự động khi xác nhận từng job.
- Chỉ quản lý phiên local; không chuyển toàn bộ cloud history hoặc hợp nhất các CODEX_HOME riêng.
- Summary có thể thiếu chi tiết, không thay thế backup project.
- Phụ thuộc app-server và chưa xác nhận mọi phiên bản/hệ điều hành.
- Lỗi continuation có thể làm cả job báo lỗi dù import đã xong; kiểm tra journal trước khi retry.

Tài liệu kỹ thuật cũ được giữ tại [docs/technical-reference.md](docs/technical-reference.md). Tài liệu đó có ghi chú lịch sử; README này là điểm bắt đầu cho người dùng mới.


## Tự đóng/mở lại desktop khi rotation

Trong popup xác nhận **Rotate ngay** hoặc **Force compact & export**, chọn **Tự đóng và mở lại Codex desktop / ChatGPT.app cho phiên này**. Tùy chọn mặc định tắt, chỉ áp dụng job thủ công được xác nhận; không tự áp dụng cho automatic rotation, repair hay Force switch.

Relay đọc PID và đường dẫn process, chỉ quản lý `/Applications/ChatGPT.app` và `/Applications/Codex.app`. App đang chạy được ghi vào journal, yêu cầu quit bằng AppleScript rồi chờ tối đa 30 giây cho helper process dừng. Không gửi SIGKILL và không tự force quit; nếu macOS từ chối quyền Automation hoặc app chưa thoát, job dừng trước compact/switch và báo lỗi.

Vì guard hiện tại yêu cầu client đóng, desktop được đóng **trước compact**, không chỉ trước bước đổi account. Sau job, Relay gửi yêu cầu mở lại đúng app ban đầu bằng `open -a`. Chỉ yêu cầu Launch Services thành công được xác nhận; chưa xác minh identity bên trong UI desktop sau mở lại. Nếu account switch còn journal hoặc job cần recovery, app không tự mở; kiểm tra account rồi mở thủ công. Nếu Relay/server bị tắt giữa chừng, không tự replay quit/reopen sau restart.

Lưu công việc trước khi xác nhận. Chạy server trong Terminal riêng và dashboard trong Safari/Chrome; đóng ChatGPT.app cũng đóng browser/chat nằm trong app đó. VS Code và CLI khác vẫn phải đóng thủ công. Code ghi trạng thái đóng/mở lại vào journal và activity log.

`code --disable-extension ...` rồi `code .` chưa được dùng để bỏ guard VS Code: chưa có xác minh rằng chuỗi lệnh đó dừng app-server của extension trong mọi cửa sổ/profile đang mở rồi bật lại đúng trạng thái. Không sửa hay disable extension đang cài.


## Ý nghĩa các nút thao tác

- **Compact & tạo handoff mới:** gọi compact thật và tạo Markdown mới cho các workflow đã chọn; giữ account, không import. Chờ client đóng; thread active bị từ chối để không cắt lượt đang chạy.
- **Compact & chuyển account:** toàn bộ compact → handoff mới → chọn đích → đổi đăng nhập → import; continuation chỉ khi đã bật.
- **Chỉ đổi account:** không compact/handoff/import, vẫn cần client đóng.
- Checkbox **Tự động rotate khi quota thấp:** chỉ thay cờ tự động, không hủy job hiện tại. Tắt được cả lúc job đang chạy.
- **Hủy phiên đang chạy:** yêu cầu dừng job và tắt tự động để tránh chạy lại; giữ dữ liệu đã hoàn tất. Không hoàn tác sửa file project.

Trong popup **Compact & chuyển account**, chọn **Tự chọn theo quota và thời điểm reset** hoặc account đích cụ thể (alias/email). Account hiện tại không nằm trong danh sách đích. Account chỉ định vẫn phải đủ quota và không bị disable; nếu không đạt thì job dừng, không fallback sang account khác. Lựa chọn áp dụng cho job thủ công hiện tại.

### Lượt reset quota còn lại

Bảng Quota Codex hiển thị số lượt reset còn lại và chi tiết hạn dùng theo múi giờ trình duyệt. Dữ liệu được đọc cùng quota qua `account/rateLimits/read` (`rateLimitResetCredits`), không gọi thao tác sử dụng reset. Mở “Hạn dùng lượt reset” để xem từng lượt, sắp theo hạn gần nhất.

Số lượng từ provider được giữ nguyên dù danh sách chi tiết bị giới hạn. “Chưa có dữ liệu” khác với 0 lượt; “Chưa được báo cáo” không có nghĩa là không hết hạn. Nếu Codex/provider chưa trả trường này, hãy cập nhật CLI và Refresh; app không suy đoán dữ liệu thiếu. Thông tin phản ánh lần đọc quota gần nhất, dùng chung lịch polling hiện tại.

Nguồn giao thức: https://learn.chatgpt.com/docs/app-server

Nút **Dùng reset sắp hết hạn** yêu cầu xác nhận account và hạn dùng, sau đó đọc lại danh sách mới trước khi gửi `account/rateLimitResetCredit/consume` với ID lượt cụ thể và idempotency key. Chỉ cho dùng khi có đủ chi tiết tất cả lượt và hạn dùng còn hợp lệ; không suy đoán lượt gần nhất từ danh sách bị thiếu. Provider quyết định cửa sổ đủ điều kiện được khôi phục. Sau kết quả xác nhận, đọc lại quota.

Ý định gửi được ghi bền vững trong `.relay-accounts/reset-operations.json` trước redemption. Nếu gián đoạn hoặc kết quả không rõ, không tự retry và khóa thêm reset của account đó qua restart. Kiểm tra lịch sử trên Codex trước khi xử lý trạng thái này; hiện chưa có UI phục hồi riêng. Không xóa journal để thử lại mù. Thao tác dùng lượt thật chỉ xảy ra khi người dùng xác nhận nút; test dùng RPC giả lập.
