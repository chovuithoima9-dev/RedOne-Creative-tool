# 🚀 Cập nhật RedOne Creative Tool & Extension (v1.6.2)

> **Bản cập nhật lớn:** Hỗ trợ tạo ảnh/video song song đa tài khoản Google Flow (Multi-Account Parallel Execution), đồng bộ hóa thời gian thực giữa nhiều Chrome Profile và nâng cấp trải nghiệm quản lý tác vụ độc lập.

---

### 🌟 Tính năng & Cải tiến nổi bật:

#### 1. 👥 Chạy song song đa tài khoản Google Flow (Multi-Account Parallel Execution)
* **Tăng gấp đôi năng suất làm việc:** Hỗ trợ mở và sử dụng cùng lúc 2 (hoặc nhiều) Profile Chrome với các tài khoản Google Flow khác nhau kết nối về cùng một Backend Tool.
* **Định tuyến chuẩn xác theo Profile:** Bấm nút **Gen** ở cửa sổ Profile Chrome nào thì task sẽ tự động được gán và thực thi chính xác bởi tài khoản Google Flow của Profile đó.
* **Hàng đợi độc lập 100%:** Mỗi tài khoản sở hữu queue thực thi, worker và khóa xử lý riêng biệt. Cả 2 tài khoản có thể cùng lúc gen ảnh, gen video mà không phải chờ đợi hay block lẫn nhau.

#### 2. 🏷️ Gán Tag tài khoản & Quản lý Task minh bạch
* Mọi task được tạo ra đều được gắn nhãn tài khoản (`account_email`) rõ ràng ngay trên giao diện quản lý task.
* Dễ dàng nhận biết task nào thuộc về tài khoản nào, tiện lợi theo dõi quota và tiến độ riêng của từng tài khoản.

#### 3. 🔄 Đồng bộ hóa thời gian thực đa Profile (Real-time Cross-Profile Sync)
* Nhờ cơ chế WebSocket Broadcast Hub, mọi thay đổi về trạng thái task (đang tạo, hoàn thành, tiến độ %) được cập nhật tức thì trên tất cả các cửa sổ Profile Chrome.
* Người dùng ở Profile 1 vẫn có thể theo dõi đầy đủ danh sách và trạng thái các task đang chạy của Profile 2 mà không hề bị xung đột giao diện.

#### 4. 👁️ Khắc phục xem chi tiết Task chéo Profile (Cross-Profile Task Inspector)
* Tự động tìm nạp dữ liệu chi tiết (`tasksStore.getOrFetch`) khi bấm vào biểu tượng "con mắt" (Inspect task) của bất kỳ task nào.
* Dù đang đứng ở Profile của tài khoản 1, bạn vẫn có thể xem đầy đủ hình ảnh, video kết quả, prompt và thông số của task được tạo bởi tài khoản 2 mà không gặp lỗi trắng trang.

#### 5. 🛡️ Cách ly môi trường Extension Bridge an toàn
* Extension **RedOne Auth Helper** ở mỗi Profile tự động định danh đúng tài khoản Flow đang đăng nhập.
* Backend phân luồng thông minh: Lệnh reCAPTCHA token hay gọi RPC của tài khoản nào sẽ chỉ gửi đến Extension của đúng Profile đó, loại bỏ hoàn toàn nguy cơ lẫn lộn token hay cookie giữa các tài khoản.

---

### 📖 Hướng dẫn sử dụng tính năng Đa tài khoản:

1. **Chuẩn bị 2 Profile Chrome:**
   * Mở Profile Chrome A ➔ Đăng nhập Google Flow tài khoản 1 (`acc1@gmail.com`).
   * Mở Profile Chrome B ➔ Đăng nhập Google Flow tài khoản 2 (`acc2@gmail.com`).
2. **Cài đặt Extension:** Đảm bảo cả 2 Profile Chrome đều đã cài đặt tiện ích **RedOne Auth Helper**.
3. **Mở giao diện Tool:**
   * Mở tab Tool (`http://localhost:8000`) trên cả Profile A và Profile B.
   * Tool sẽ tự động nhận diện tài khoản Flow tương ứng của từng tab.
4. **Tạo Task song song:**
   * Bấm Gen ở Profile A ➔ Task chạy bằng `acc1@gmail.com`.
   * Bấm Gen ở Profile B ➔ Task chạy bằng `acc2@gmail.com`.
   * Cả 2 task chạy song song cùng lúc, hiển thị đầy đủ trên cả 2 màn hình!

---

### 📥 Cách cập nhật:
1. **Với Tool:** Khởi động lại Tool RedOne để nhận diện phiên bản `v1.6.2`.
2. **Với Extension:** Vào `chrome://extensions/` ➔ Bật "Chế độ dành cho nhà phát triển" (Developer mode) ➔ Bấm **Cập nhật** (hoặc bấm nút **Tải lại ↻** trên tiện ích **RedOne Auth Helper**).
