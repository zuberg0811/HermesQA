// Sao chép thành firebase-config.js và điền giá trị từ Firebase console
// (Project settings -> Your apps -> SDK setup and configuration -> Config).
// Web API key của Firebase KHÔNG phải bí mật: quyền đọc/ghi do firestore.rules quyết định.
window.HERMESQA = {
  firebase: {
    apiKey: "AIza...",
    authDomain: "hermesqa-demo.firebaseapp.com",
    projectId: "hermesqa-demo",
    appId: "1:1234567890:web:abcdef",
  },
  // Máy chủ FastAPI chạy HermesQA (có app.demo gắn vào). Để trống nếu trang web và API cùng origin.
  apiBase: "http://localhost:8000",
  // Tên collection — chỉ đổi khi đổi FIRESTORE_COLLECTION / FIRESTORE_JOBS_COLLECTION phía máy chủ
  collections: { runs: "hermesqa_runs", eval: "hermesqa_eval", jobs: "hermesqa_demo_jobs" },
};
