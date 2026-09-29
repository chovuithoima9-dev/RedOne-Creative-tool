# 🚀 Cập nhật RedOne Creative Tool & Extension (v1.6.1)

### 🌟 Tính năng & Sửa lỗi nổi bật:

#### 1. 🍌 Sửa lỗi chọn Model tạo ảnh (Nano Banana Pro / Nano Banana 2)
* **Khớp mã chuẩn của Google Flow:** Đã ánh xạ chính xác `nano_banana_pro` sang `GEM_PIX_2` và `nano_banana_2` sang `NARWHAL`.
* Triệt tiêu hoàn toàn lỗi chọn Banana Pro nhưng lại bị tạo bằng Banana 2 Lite (`HARBOR_SEAL`).

#### 2. ⚡ Tối ưu tạo 1 hình duy nhất (Tiết kiệm Quota & Giảm Rate Limit)
* Thay vì gửi lệnh tạo 4 ảnh cùng lúc trên Google Flow rồi chỉ lấy 1 ảnh, Tool nay gửi đúng **1 candidate** cho mỗi prompt.
* Tiết kiệm 4x dung lượng và quota, đồng thời giảm thiểu tối đa hiện tượng Google trả về lỗi nghẽn lệnh (Rate Limit / Throttling).

#### 3. 🖼️ Chống Upload ảnh tham chiếu trùng lặp
* Tích hợp bộ nhớ đệm cache và khóa đồng bộ (`_UPLOAD_CACHE` & `_UPLOAD_LOCKS`) ở cấp Bridge client.
* Khi chạy Storyboard hoặc nhiều phân cảnh dùng chung một ảnh tham chiếu, ảnh chỉ upload lên Google Flow **đúng 1 lần duy nhất**, loại bỏ hoàn toàn các ảnh upload trùng lặp trong dự án.

#### 4. 🎬 Hỗ trợ toàn diện Model Video Lite LP
* Bổ sung đầy đủ cho model Lite LP ở cả 2 chế độ:
  * **Tham chiếu (R2V / Ingredients)**
  * **Khung hình đầu / cuối (I2V / First-Last frame)**
* Đồng bộ quy trình tạo video, storyboard và upscale đồng nhất với G-Labs Studio.

#### 5. 🔔 Tinh chỉnh thông báo lỗi nghẽn Google
* Rút gọn và chuẩn hóa thông báo rõ ràng: `"Google tạm nghẽn vì gửi yêu cầu quá nhanh liên tục trong thời gian ngắn (Rate Limit / Throttled)"`.

---

### 📥 Cách cập nhật:
1. **Với Tool:** Khởi động lại Tool RedOne để áp dụng phiên bản `v1.6.1`.
2. **Với Extension:** Vào `chrome://extensions/` ➔ Bật "Chế độ dành cho nhà phát triển" (Developer mode) ➔ Bấm **Cập nhật** (hoặc bấm nút **Tải lại ↻** trên tiện ích **RedOne Auth Helper**).
