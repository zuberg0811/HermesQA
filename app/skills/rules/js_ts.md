### JavaScript / TypeScript / React
**Báo:**
- XSS: gán `innerHTML`/`outerHTML`, `dangerouslySetInnerHTML`, `document.write` với dữ liệu không tin cậy; `eval`/`new Function`.
- `==`/`!=` lỏng khi hai vế có thể khác kiểu; thiếu `await` trên Promise (kết quả bị dùng như giá trị);
  Promise không được bắt lỗi.
- TypeScript: ép `as any`/`as unknown as T` để bỏ qua kiểm tra kiểu ở nơi giá trị có thể `undefined`/`null`.
- React: thiếu `key` khi render danh sách, `key` là index trên danh sách thay đổi thứ tự, hook gọi có điều kiện,
  `useEffect` thiếu dependency dẫn tới dữ liệu cũ.
- Bí mật/token trong mã phía client; nối chuỗi trong vòng lặp lớn; N lần gọi mạng tuần tự trong vòng lặp.

**Không báo:**
- `any` trong file khai báo kiểu, mock, hoặc code test.
- Phong cách (dấu chấm phẩy, nháy đơn/kép, thứ tự import) — việc của linter/formatter.
- Thiếu memo/`useCallback` khi không có bằng chứng về vấn đề hiệu năng.
