# 📘 RedOne Creative — Hướng dẫn sử dụng

> Tool tạo **ảnh & video AI** cho nội bộ RedOne:
> **Google Flow** (Veo 3.1 + Nano Banana) và **Shakker.ai** (Stable Diffusion + LoRA),
> kèm **Tạo Storyboard** và **Ý Tưởng → Prompt** chạy bằng Gemini.
>
> **Phiên bản:** v1.3.3 · **Hệ điều hành:** Windows 10/11

---

## 📑 Mục lục

1. [Cài đặt & chạy tool](#1-cài-đặt--chạy-tool)
2. [Đăng nhập tool (bắt buộc — email @redone.vn)](#2-đăng-nhập-tool-bắt-buộc--email-redonevn)
3. [Kết nối để tạo: cài Chrome Extension (bắt buộc)](#3-kết-nối-để-tạo-cài-chrome-extension-bắt-buộc)
4. [Tạo ảnh AI (Google Flow)](#4-tạo-ảnh-ai-google-flow)
5. [Ảnh Shakker (mới)](#5-ảnh-shakker-mới)
6. [Tạo video (Text→Video & Image→Video)](#6-tạo-video)
   - 🎬 Tạo Storyboard (mới v1.3.0)
   - ✍️ Ý Tưởng → Prompt (mới v1.3.0)
7. [Gen song song: Shakker + Flow cùng lúc](#7-gen-song-song-shakker--flow-cùng-lúc)
8. [Khi gặp lỗi: thông báo & Gen lại](#8-khi-gặp-lỗi-thông-báo--gen-lại)
9. [Quản lý task & tải file](#9-quản-lý-task--tải-file)
10. [Cài đặt](#10-cài-đặt)
    - 🔑 Gemini API Key — lấy & nhập nhiều key (tự xoay vòng khi hết quota)
11. [Cập nhật tool](#11-cập-nhật-tool)
12. [Lỗi thường gặp](#12-lỗi-thường-gặp)
13. [Liên hệ / Báo lỗi](#13-liên-hệ--báo-lỗi)

---

## 1. Cài đặt & chạy tool

### Yêu cầu
- Windows 10 hoặc 11
- Kết nối internet ổn định
- Một tài khoản Google **@redone.vn** (để đăng nhập tool)
- **Google Chrome** — **bắt buộc** (tool tạo ảnh/video qua extension trên Chrome, xem mục 3)

### Tải về và chạy

1. Vào https://github.com/chovuithoima9-dev/RedOne-Creative-tool/releases
2. Tải file `RedOne-Creative-vX.X.X-win64.zip` ở mục **Latest release**
3. **Chuột phải** vào file zip → **Extract All…** (BẮT BUỘC giải nén, đừng chạy trong zip)
4. Mở folder vừa giải nén → **double-click `RedOne Creative.exe`**
5. Đợi 5–10 giây → trình duyệt mặc định tự mở `http://127.0.0.1:8000`

⚠️ **Windows Defender chặn** → bấm **More info → Run anyway**.
⚠️ **Báo `python***.dll missing`** → bạn chưa giải nén hoặc đã kéo `.exe` ra khỏi folder. Giải nén lại đầy đủ, chạy `.exe` **từ trong** folder.
⚠️ **Báo `MSVCP140.dll missing`** → cài Visual C++ Redistributable: https://aka.ms/vs/17/release/vc_redist.x64.exe

---

## 2. Đăng nhập tool (bắt buộc — email @redone.vn)

Ngay khi mở, tool hiện **trang đăng nhập**. Đây là cổng bảo vệ: **chỉ tài khoản Google có đuôi `@redone.vn`** mới vào được.

1. Bấm **"Đăng nhập với Google"**
2. Chọn / nhập tài khoản Google công ty (`...@redone.vn`)
3. Cho phép quyền → tool tự quay lại và mở giao diện chính

✅ Phiên đăng nhập giữ **30 ngày** — không phải đăng nhập lại mỗi lần mở.
✅ Một mail @redone.vn đăng nhập được trên **nhiều máy** cùng lúc (mỗi máy 1 phiên riêng).
❌ Đăng nhập bằng Gmail thường (không phải @redone.vn) → bị từ chối.

> 💡 Đăng nhập tool ≠ tài khoản dùng để tạo ảnh. Việc tạo ảnh/video dùng tài khoản Google đăng nhập trong **Chrome** (mục 3) và tài khoản **Shakker** (mục 5) — tách biệt với mail @redone.vn đăng nhập tool.

---

## 3. Kết nối để tạo: cài Chrome Extension (bắt buộc)

Từ v1.2.0, tool tạo ảnh/video **hoàn toàn qua Chrome Extension Bridge** — token + cookies lấy từ **Chrome thật** của bạn nên Google không gắn cờ bot (giảm 403), và dùng **quota miễn phí** của Google Labs Flow. *(Các chế độ Vertex AI và Playwright/Cloak cũ đã được gỡ bỏ.)*

### Cài extension (làm 1 lần)

1. Sau khi giải nén zip, mở folder **`extension\`** đi kèm
2. Mở **Chrome thật** → vào `chrome://extensions/` → bật **Developer mode** (góc trên phải)
3. Bấm **Load unpacked** → chọn folder `extension\` → extension **"RedOne Auth Helper"** (icon R đỏ) xuất hiện

### Đăng nhập các dịch vụ trong Chrome

- **Để tạo ảnh/video Google Flow:** mở tab https://labs.google/fx/tools/flow → đăng nhập tài khoản Google muốn dùng để gen.
- **Để tạo Ảnh Shakker:** mở tab https://www.shakker.ai → đăng nhập tài khoản Shakker.

> Extension tự đồng bộ token/cookies về tool sau vài giây. Bấm vào icon extension để xem trạng thái "Đã kết nối".

### Thêm account Google vào tool (cho Flow)

Vào tab **Tài Khoản → + Thêm account** → nhập email account vừa login trong Chrome → bấm **Check** để nạp tín dụng/trạng thái.

> Mỗi máy độc lập, cookies không sync giữa máy (cố ý — tránh Google phát hiện 1 account dùng nhiều máy).

---

## 4. Tạo ảnh AI (Google Flow)

### 4.1. Mở tab **Tạo Ảnh** (sidebar → nhóm SÁNG TẠO)

### 4.2. Cấu hình (panel trái)

| Mục | Lựa chọn |
|---|---|
| **Tên task** | Vd: `chan_dung_studio` → file lưu ở `outputs/image/<ngày>/chan_dung_studio/` |
| **Model** | **Nano Banana 2** (nhanh, rẻ) hoặc **Nano Banana Pro** (chất lượng cao hơn) |
| **Tỉ lệ** | `1:1`, `16:9`, `9:16`, `4:3`, `3:4` |
| **Số ảnh / prompt** | 1–8 (vd 1 prompt × 4 = 4 biến thể) |
| **Số luồng song song** | 1–3 |
| **Ảnh tham chiếu** | (tùy chọn) kéo thả ảnh để model bám phong cách/nhân vật |

### 4.3. Nhập prompt (panel phải)

- **Bulk paste**: dán nhiều prompt, cách nhau **1 dòng trắng** → bấm **Áp dụng**
- **Từng dòng**: bấm **+ Thêm prompt**
- **Import .txt**: mỗi dòng = 1 prompt

> ✍️ **Quan trọng:** viết prompt **mô tả ảnh cụ thể** (chủ thể, bối cảnh, phong cách). Prompt quá ngắn / kiểu chào hỏi ("xin chào", "test") sẽ bị model hiểu là hội thoại và **không ra ảnh**.
> Ví dụ tốt: `một con mèo tam thể ngồi trên ghế sofa, phong cách ảnh chụp thật, ánh sáng studio`.

### 4.4. Bấm **Tạo ảnh**

Gallery bên phải hiện các card: **Đang chờ → Đang tạo → Hoàn thành**. Mỗi ảnh xong hiện thumbnail (click để zoom), có nút **Tải** + **Copy prompt**.

File lưu ở `outputs\image\<ngày>\<tên_task>\item_X.png` (mặc định auto-lưu).

### 4.5. Upscale 2K / 4K

Tick ảnh → toolbar hiện **Nâng cấp 2K / 4K** (dùng Labs Flow upscale). Trạng thái hiện ngay trên ảnh (Chờ → Đang upscale → ✓ 2K/4K) + tiến trình "đang upscale N/M" trên thanh công cụ. Xong, bấm **Tải 2K/4K** trên từng ảnh để lưu về (không tự tải).

---

## 5. Ảnh Shakker (mới)

Tab **Ảnh Shakker** (sidebar → nhóm SÁNG TẠO) sinh ảnh hàng loạt qua **Shakker.ai** — engine Stable Diffusion + kho **LoRA** khổng lồ. Phù hợp ảnh phong cách hóa, anime, nhân vật theo LoRA — bổ sung bên cạnh Google Flow.

> 🔑 **Điều kiện:** đã cài extension và **đăng nhập shakker.ai trong Chrome** (mục 3). Trang sẽ hiện email + **power** (tín dụng Shakker) còn lại ở góc trên.

### 5.1. Cấu hình (panel trái)

| Mục | Lựa chọn |
|---|---|
| **Tên task** | Vd: `anime_nhanvat` |
| **Model** | Bấm để mở danh sách → **tìm kiếm theo từ khóa**, chọn 1 model (checkpoint) |
| **LoRA** | Thêm **nhiều LoRA** (phong cách). Mỗi LoRA chỉnh được **weight** (độ ảnh hưởng) |
| **Tỉ lệ khung hình** | 7 lựa chọn: `1:1`, `3:4`, `2:3`, `9:16`, `4:3`, `3:2`, `16:9` |
| **Ảnh tham chiếu** | (tùy chọn) **1 ảnh** (img2img) + thanh **độ giống** (cao = bám ảnh gốc hơn) |
| **Negative prompt** | (tùy chọn) mô tả thứ KHÔNG muốn có trong ảnh |
| **Số ảnh / prompt** | 1–**4** (tối đa 4 ảnh mỗi prompt) |

### 5.2. Nhập prompt (panel phải)

Giống tab Tạo Ảnh: **Bulk paste** (cách nhau 1 dòng trắng), thêm từng dòng, hoặc **Import .txt**. Nên viết prompt **tiếng Anh** để Shakker cho kết quả tốt nhất.

### 5.3. Bấm **Tạo ảnh**

- Shakker tạo **lần lượt từng prompt** (tài khoản giới hạn 1 lượt/lúc) — gallery hiện đúng trạng thái: 1 ảnh **Đang tạo**, còn lại **Đang chờ**.
- Ảnh xong hiện thumbnail + nút **Tải** / **Copy prompt**. Lưu ở `outputs\image\shakker_<id>\...`.

> 💳 Shakker tính bằng **power** (không phải credit Google). Hết power → nạp tại shakker.ai hoặc đổi sang tài khoản Shakker khác (đăng nhập account khác trong Chrome). Power còn lại tự cập nhật sau mỗi đợt gen.

---

## 6. Tạo video

### 6.1. Tab **Tạo Video** → chọn **Text → Video** hoặc **Image → Video**

### 6.2. Cấu hình

| Mục | Lựa chọn |
|---|---|
| **Model** | **Lite** (rẻ nhất) · **Fast** (cân bằng) · **Quality** (đẹp nhất) · **Lite [LP]** (hàng đợi free) · **Omni Flash** — đều qua Labs Flow |
| **Tỉ lệ** | `16:9` (ngang) hoặc `9:16` (dọc) |
| **Độ dài** | tối đa 8 giây / clip |
| **Số luồng song song** | 1–3 |

### 6.3. Text → Video (T2V)

Nhập prompt. Mẹo cho Veo: ưu tiên tiếng Anh, mô tả **shot type + camera move + scene + style**.
Ví dụ: `Medium shot, static camera, a woman in red dress walking through autumn forest, cinematic lighting`.

### 6.4. Image → Video (I2V)

1. Kéo thả **nhiều ảnh** vào dropzone (mỗi ảnh = 1 video, tự đánh số #1, #2…)
2. Kéo-thả để đổi thứ tự nếu cần
3. Nhập prompt theo thứ tự ảnh (Ảnh #1 ↔ Prompt #1), hoặc 1 prompt dùng chung

### 6.5. Bấm **Bắt đầu render**

Mỗi video ~30–90 giây. Xong có **player ngay trong card** + nút **Tải** (MP4). File ở `outputs\video\<ngày>\<tên_task>\item_X.mp4`.

> ⏳ Giữa mỗi đợt gen, tool **nghỉ ngẫu nhiên 5–10 giây** (chỉnh được ở Cài đặt) để tránh bị Google rate-limit.

---

## 🎬 Tạo Storyboard (mới v1.3.0)

Tab **Tạo Storyboard** (sidebar → SÁNG TẠO): từ **một ý tưởng** + (tùy chọn) **ảnh tham chiếu**, AI tự **chia thành nhiều phân cảnh**, viết prompt cho từng cảnh rồi **gen ảnh hàng loạt** — ra một bộ storyboard hoàn chỉnh.

> 🔑 Cần **Gemini API Key** (nên nhập nhiều key — xem **mục 10.1**) + đã cài extension & đăng nhập labs.google trong Chrome (mục 3).

**Cấu hình (panel trái):**

| Mục | Lựa chọn |
|---|---|
| **Ý tưởng** | Mô tả tổng (vd: "cảnh thiên nhiên rừng xanh, nhiều góc khác nhau") |
| **Ảnh tham chiếu** | (tùy chọn) tối đa **10 ảnh** — AI bám chủ thể/bối cảnh/phong cách của chúng để các cảnh đồng nhất |
| **Model gen ảnh** | Nano Banana Pro / Nano Banana 2 / Imagen 4 |
| **Tỉ lệ** | 1:1, 16:9, 9:16, 4:3, 3:4 |
| **Số cảnh** | bao nhiêu cũng được |
| **Số luồng song song** | số ảnh gen đồng thời (cao quá dễ bị 429) |

**Sử dụng:** bấm **Tạo Storyboard** → AI viết prompt từng cảnh → gen ảnh dần. Mỗi card = **SCENE N** (ảnh + prompt), có nút **Tải** + **Copy prompt**.

**Upscale, Gen lại & gửi sang Tạo Video:**
- Tick các cảnh → thanh chọn hiện **Nâng cấp 2K / 4K**, **🔄 Gen lại** (tạo lại cảnh đã chọn), và **▶ Gửi sang Video (I2V)**.
- **Gửi sang Video (I2V)** → chuyển các cảnh **đã chọn** sang tab Tạo Video ở chế độ Image→Video (mỗi ảnh = khung đầu của 1 video). **Cảnh nào đã upscale thì gửi bản đã upscale.**
- Cảnh lỗi: dùng nút **"Gen lại N lỗi"** ở đầu khu kết quả.

---

## ✍️ Ý Tưởng → Prompt (mới v1.3.0)

Tab **Ý Tưởng → Prompt** (đổi tên từ "Ý Tưởng → Video"): dán **kịch bản / ý tưởng** → AI sinh **prompt chuẩn** cho video hoặc ảnh.

- Toggle trên cùng: **Tạo Video** (mặc định) hoặc **Tạo Ảnh**.
- Chế độ **Ảnh**: upload được **ảnh tham chiếu** (chủ thể / bối cảnh / style) + chọn số lượng prompt.
- Mỗi prompt (và "tất cả") gửi được sang **Tạo Video** *hoặc* **Tạo Ảnh**.

> 🔑 Cần **Gemini API Key** (nên nhập nhiều key — xem **mục 10.1**). Dùng Gemini 3.5 Flash (model Pro cần API key đã bật billing).

---

## 7. Gen song song: Shakker + Flow cùng lúc

Từ v1.2.0, task **Shakker** và task **Flow (ảnh/video)** chạy trên **2 luồng độc lập** → **đồng thời**, không phải chờ nhau.

- Bạn có thể bấm gen ở tab **Tạo Ảnh / Tạo Video** rồi sang tab **Ảnh Shakker** bấm gen ngay — cả hai cùng chạy.
- Trong **cùng một loại**, task vẫn xếp hàng tuần tự (các task Flow chờ nhau; các task Shakker chờ nhau) — đúng như trước.

---

## 8. Khi gặp lỗi: thông báo & Gen lại

Tool **không hủy hàng loạt** khi 1 prompt lỗi — mỗi prompt chạy độc lập, lỗi cái nào chỉ cái đó.

### 8.1. Thông báo lỗi rõ ràng

Card lỗi hiện **thông báo tiếng Việt dễ hiểu** (rê chuột để xem chi tiết kỹ thuật), nêu rõ **nguyên nhân**, **tạm thời hay vĩnh viễn**, và **việc cần làm**. Các lỗi hay gặp:

| Thông báo | Ý nghĩa | Cách xử lý |
|---|---|---|
| "Google tạm chặn… (reCAPTCHA)" | Gửi quá nhiều/nhanh → bị nghi bot. **Tạm thời** | Tool tự thử lại. Bị liên tục: giảm luồng, tăng cooldown, nghỉ vài phút, hoặc đổi account |
| "Hết lượt tạo… hôm nay" | Hết quota miễn phí trong ngày | Đổi tài khoản Google khác, hoặc chờ sang mai |
| "…có người nổi tiếng…" | Prompt/ảnh có người thật nổi tiếng | Bỏ tên/ảnh người nổi tiếng (gen lại y nguyên vẫn lỗi) |
| "Nội dung bị Google chặn (chính sách)" | Prompt/ảnh nhạy cảm, bạo lực, bản quyền | Sửa prompt trung tính hơn |
| "Phiên đăng nhập… hết hạn" | Session Google trong Chrome đã hết | Mở Chrome đăng nhập lại labs.google, hoặc đổi account |
| **(Shakker)** "Hết power" | Tài khoản Shakker hết tín dụng | Nạp power tại shakker.ai hoặc đổi account Shakker |
| **(Shakker)** "Phiên Shakker hết hạn" | Token Shakker hết | Mở shakker.ai trong Chrome đăng nhập lại |

### 8.2. Gen lại (có ưu tiên)

- **Gen lại các mục đã chọn**: tick checkbox các card **đã xong** → trên **thanh chọn** bấm **🔄 Gen lại** → tạo lại **đè tại chỗ**, ngay trong task đang mở (không tạo task mới trên hàng chờ). Áp dụng cho **Tạo Ảnh, Tạo Video, Tạo Storyboard, Ảnh Shakker**.
- **Gen lại tất cả lỗi**: nút **"Gen lại N lỗi"** ở đầu khu Kết quả (gen lại mọi prompt đang lỗi).
- **⚡ Ưu tiên**: lệnh Gen lại **chen lên trước** hàng gen thường — kể cả khi bấm sau khi 1 task khác đã chạy. Thứ tự: **Gen lại › Upscale 2K/4K › Gen thường**.
- Card **đang upscale** thì tạm thời **không Gen lại được** (tự bỏ qua, tránh xung đột).
- ✅ Tất cả **hoạt động ngay cả khi task chưa chạy xong**; prompt đã thành công được giữ nguyên (Flow lẫn Shakker).

---

## 9. Quản lý task & tải file

### 9.1. Tab **Quản lý Task** (sidebar → Hệ Thống)

Bảng liệt kê mọi task (Ảnh / Video / Shakker): tên, loại, trạng thái, tiến độ, thời gian. Mỗi dòng có:
- 📁 **Mở folder** — mở thư mục output trong File Explorer
- 👁 **Xem** — nhảy tới trang tạo với đúng task đó
- 🔄 **Retry** — chạy lại task lỗi/hủy (giữ item đã xong)
- ⏹ **Hủy** — dừng task đang chạy

### 9.2. Đa-chọn trên gallery

Tick checkbox góc các card (đã xong) → thanh chọn hiện:
- **🔄 Gen lại** — tạo lại các mục đã chọn (đè tại chỗ, ưu tiên — xem mục 8.2)
- **▶ Gửi sang Video (I2V)** — (Tạo Ảnh / Storyboard) đẩy ảnh đã chọn sang tab Tạo Video làm khung đầu; cảnh nào đã upscale thì gửi bản đã upscale
- **Tải về đã chọn** (zip nếu chọn >1 file)
- **Nâng cấp 2K / 4K** — (ảnh) upscale các ảnh đã chọn
- **Bỏ khỏi danh sách** (file gốc vẫn còn trên đĩa)
- (tab Video) **Xóa watermark**

### 9.3. Xóa danh sách

Nút **Xóa danh sách** xóa các task khỏi giao diện (file trong `outputs\` **không** bị xóa). Hữu ích khi danh sách cũ load chậm/ảnh hỏng.

---

## 10. Cài đặt

Sidebar → **Hệ Thống → Cài Đặt**.

### 10.1. 🔑 Gemini API Key (lấy & nhập nhiều key)

Các tính năng **tạo prompt bằng AI** — **Tạo Storyboard**, **Ý Tưởng → Prompt**, và phân tích **YouTube / kịch bản / ảnh** — chạy bằng **Google Gemini**, nên cần **API key** (miễn phí).

**Bước 1 — Lấy API key:**

1. Vào **https://aistudio.google.com/apikey** → đăng nhập **một tài khoản Google bất kỳ** (không cần `@redone.vn`).
2. Bấm **"Create API key"** (Tạo khóa API) → chọn project có sẵn hoặc tạo mới.
3. Copy chuỗi key dạng **`AIza...`**.

**Bước 2 — Nhập vào tool:**

1. Cài Đặt → ô **"Gemini API Keys"**.
2. Dán key vào — **mỗi key một dòng** → bấm **Lưu**.
3. Bấm **Test Gemini** để kiểm tra key chạy được. Nút **👁** để hiện/ẩn key.

**💡 Nhập NHIỀU key để không lo hết lượt:**

Gemini bản free có **giới hạn lượt dùng mỗi ngày**. Nhập nhiều key (mỗi dòng 1 key) → khi **1 key hết hạn mức, tool tự chuyển sang key kế tiếp**, gen tiếp không gián đoạn. Khi **hết tất cả** key, tool báo lỗi rõ ràng: *"Tất cả N Gemini API key đều đã hết hạn mức (quota)… → thêm key mới hoặc đợi reset"*.

> ⚠️ **QUAN TRỌNG: mỗi key nên lấy từ một TÀI KHOẢN GOOGLE KHÁC NHAU.**
> Nhiều key tạo trên **cùng một tài khoản** Google dùng **chung một hạn mức** → xoay vòng sẽ **vô nghĩa** (key nào cũng hết cùng lúc).
> 👉 Tạo vài tài khoản Google riêng, mỗi tài khoản lấy 1 key, rồi dán hết vào (mỗi dòng 1 key).

> 🔄 Quota free thường **reset theo ngày** — hôm sau các key dùng lại được bình thường.

### 10.2. Các cài đặt khác

| Setting | Tác dụng |
|---|---|
| **Chế độ kết nối** | Cố định **Chrome Extension Bridge** — hiển thị trạng thái extension đã kết nối hay chưa |
| **Khoảng nghỉ giữa các đợt gen** | Random min–max giây (mặc định 5–10s) — tăng lên nếu hay bị 429 |
| **Tỉ lệ / Chất lượng mặc định** | Áp dụng khi mở trang tạo mới |
| **Tự lưu vào outputs/** | Bật = lưu vĩnh viễn; Tắt = lưu tạm (tự xóa sau 1 tuần) |
| **Kiểm tra cập nhật** | Kiểm tra bản mới trên GitHub |

---

## 11. Cập nhật tool

- Tool tự kiểm tra bản mới trên GitHub. Khi có, hiện **banner cập nhật** (trong ~5 phút, hoặc bấm Cài Đặt → "Kiểm tra cập nhật").
- Bấm **"Tải xuống & cài đặt"** → tool tự tải zip, giải nén, cài đè và khởi động lại.
- **`data\` và `outputs\` được giữ nguyên** → tài khoản, cài đặt, ảnh/video đã tạo không mất.

> ⚠️ **Sau khi cập nhật: reload lại Extension.** Bản cập nhật có thể kèm extension mới, nhưng Chrome **không tự nạp lại** extension đã "Load unpacked". Vào `chrome://extensions/` → tìm **"RedOne Auth Helper"** → bấm nút **↻ Reload** → kiểm tra version đã lên đúng → rồi refresh lại tab **labs.google** và **shakker.ai**. (Tool sẽ tự hiện banner nhắc việc này sau mỗi lần cập nhật.)

---

## 12. Lỗi thường gặp

| Lỗi | Cách fix |
|---|---|
| `python***.dll missing` / `MSVCP140.dll missing` | Giải nén zip đầy đủ + cài VC++ Redist (link ở mục 1) |
| Đăng nhập tool bị từ chối | Phải dùng email **@redone.vn**, không phải Gmail thường |
| Gen không ra gì / "Extension chưa kết nối" | Chưa cài extension hoặc chưa mở/đăng nhập tab labs.google (Flow) / shakker.ai (Shakker) trong Chrome — xem mục 3 |
| Ảnh ra **chữ thay vì hình** | Prompt quá ngắn/giống lời chào → mô tả ảnh cụ thể hơn |
| "Google tạm chặn (reCAPTCHA)" liên tục | Giảm số luồng, tăng cooldown (Cài đặt), nghỉ vài phút, hoặc đổi account Google |
| "Hết lượt tạo hôm nay" (429) | Đổi account Google khác / chờ sang mai |
| "…người nổi tiếng…" | Bỏ tên/ảnh người nổi tiếng khỏi prompt |
| Shakker báo "hết power" | Nạp power tại shakker.ai hoặc đổi account Shakker |
| Storyboard / Ý Tưởng→Prompt báo "tất cả API key hết hạn mức" | Hết quota Gemini ngày → thêm key (từ **tài khoản Google khác**) ở Cài đặt **mục 10.1**, hoặc đợi sang mai |
| "Chưa cấu hình Gemini API key" | Chưa nhập key → Cài đặt **mục 10.1** dán key (mỗi dòng 1 key) → Lưu |
| Banner cập nhật không hiện | Đợi 5 phút (cache) hoặc bấm "Kiểm tra cập nhật" |
| Port 8000 đang bận | Có instance khác đang chạy → tắt qua Task Manager (`RedOne Creative.exe`) |
| Ảnh cũ hiện lỗi/404 trong gallery | Bấm **Xóa danh sách** (file gốc không bị xóa) |

### Log file
Mọi log ở `data\app.log` (cùng folder exe). Báo lỗi cho dev: gửi **50 dòng cuối** file này.

---

## 13. Liên hệ / Báo lỗi

- **GitHub**: https://github.com/chovuithoima9-dev/RedOne-Creative-tool
- **Issues**: https://github.com/chovuithoima9-dev/RedOne-Creative-tool/issues
- **Releases**: https://github.com/chovuithoima9-dev/RedOne-Creative-tool/releases

Khi báo lỗi kèm: (1) version tool (Cài Đặt → About), (2) 50 dòng cuối `data\app.log`, (3) screenshot lỗi, (4) các bước trước khi gặp lỗi.
