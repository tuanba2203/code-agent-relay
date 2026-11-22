# Antigravity IDE · đổi tài khoản qua Keychain

Từ Antigravity 2.6.0, token đăng nhập không nằm trong thư mục profile mà nằm
trong macOS Keychain, mục **service `gemini` / account `antigravity`**. Chỉ copy
thư mục dữ liệu IDE vẫn có thể dính tài khoản cũ.

Relay dùng **cách B**: đóng IDE, lưu token đang sống, ghi token của tài khoản
kia vào Keychain (và file fallback của Antigravity), rồi mở lại IDE. Mỗi lúc
chỉ một tài khoản chạy được.

Tham khảo cách làm (không gọi các binary đó lúc runtime):
[agy-auth](https://github.com/fikrilal/agy-auth) và
[Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager).

## Dashboard

Mục **Tài khoản** trên tổng quan liệt kê snapshot Antigravity cạnh bảng Codex:
gói, hạn mức 5 giờ/tuần còn lại, và thời điểm reset nếu Google báo cáo. Quota
đọc từ snapshot token (không cần đổi Keychain) qua cùng API Code Assist mà
Antigravity IDE / VS Code dùng: `loadCodeAssist` và `retrieveUserQuotaSummary`
trên host `daily-cloudcode-pa` (số liệu realtime theo nhóm Gemini, Claude/GPT,
Argon). Access
token hết hạn được làm mới bằng client ID public của Antigravity; client secret
được quét từ `language_server` của app đã cài (không nhúng trong source Relay).
Cần `/Applications/Antigravity.app`. Đổi tài khoản và lưu snapshot ở sidebar
**Antigravity**. Nút **↻ Quota** trên từng hàng, hoặc **Đọc quota** cho tất cả,
chờ Google trả số liệu. Mỗi nhóm hạn mức (Gemini, Claude/GPT, Argon) là một
pool riêng; không cộng thành một tổng.

1. Đăng nhập tài khoản A trong Antigravity IDE.
2. Bấm **Lưu phiên Keychain hiện tại** (có thể đặt alias).
3. Trong IDE, đăng nhập tài khoản B, rồi lưu lần nữa.
4. Chọn profile và **Đổi sang tài khoản này**. Mặc định Relay gửi quit cho
   `/Applications/Antigravity.app`, ghi Keychain, rồi `open -a`.

Không force kill. Nếu macOS từ chối Automation hoặc IDE không thoát, thao tác
dừng trước khi ghi Keychain.

## Token là phiên đăng nhập sẵn

Snapshot token trong `.relay-accounts/antigravity/` gần như tương đương việc
đã đăng nhập tài khoản đó. Ai đọc được file hoặc Keychain đều dùng được phiên.

Relay:

- tạo vault với quyền **0700**, file snapshot **0600**, owner là user hiện tại
- từ chối đường dẫn iCloud Drive / `Mobile Documents` / `CloudStorage`
- ghi marker `.nosync` và xattr loại khỏi iCloud File Provider / Time Machine
- không đưa token vào API, log hay HTML
- ghi Keychain bằng `/usr/bin/security` của hệ thống; ACL chỉ thêm
  `/Applications/Antigravity.app` và `/usr/bin/security`
- **không** dùng cờ `-A` (mọi app đọc được không cảnh báo)
- **không** ủy quyền Keychain cho agy-auth, Antigravity-Manager hay CLI thứ ba

Nếu cài tool bên thứ ba, đọc mã nguồn của nó trước khi bấm Allow trên hộp
thoại Keychain.

## CLI

```sh
python3 antigravity_provider.py capture --name 'Google Work'
python3 antigravity_provider.py list
python3 antigravity_provider.py switch PROFILE_ID --relaunch
python3 antigravity_provider.py restore
```

File fallback được ghi (cùng nội dung JSON, quyền 0600):

- `~/.gemini/antigravity-cli/antigravity-oauth-token`
- `~/.gemini/jetski-standalone-oauth-token`

Relay không ghi `~/.gemini/oauth_creds.json` để tránh đè phiên Gemini CLI.

## Giới hạn

- Chỉ macOS. Keychain là nguồn sự thật với IDE 2.6+.
- Google đổi cách lưu token thì snapshot có thể ngừng hiệu lực.
- Access token hết hạn: Relay làm mới khi đọc quota nếu còn refresh token và
  quét được client secret từ Antigravity.app. IDE cũng tự refresh khi mở.
- Xóa snapshot trên Relay không thu hồi phiên Google.
- Bản HTML offline không đụng Keychain.
