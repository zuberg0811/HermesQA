### File test
**Báo:**
- Test bị `skip`/`xfail`/comment out/xoá mà không có lý do ghi rõ.
- Assertion bị xoá hoặc bị làm yếu đi (so sánh giá trị cụ thể đổi thành chỉ kiểm tra "không None"/"không lỗi").
- Test không còn assertion nào (chỉ gọi hàm); test luôn pass bất kể kết quả.
- Test phụ thuộc thời gian thực, mạng thật, thứ tự chạy.

**Không báo:**
- `assert` trong test (đó là cách viết chuẩn), số "ma thuật" trong dữ liệu test, thiếu docstring.
- Yêu cầu thêm ca kiểm thử cho hành vi mà diff này không thay đổi.
