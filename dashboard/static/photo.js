// Profile photo upload: ask the app for a pre-signed S3 POST (the app asks the
// API), then send the image straight to S3. The image never passes through
// the web server or API Gateway.
(function () {
  const form = document.getElementById("photo-form");
  if (!form) return;
  const status = document.getElementById("photo-status");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    const file = document.getElementById("photo-file").files[0];
    if (!file) return;
    status.textContent = "Uploading…";
    try {
      const ask = new FormData();
      ask.append("csrf_token", form.querySelector("[name=csrf_token]").value);
      ask.append("content_type", file.type);
      const presign = await fetch(window.MEDISLOT_PHOTO_URL, { method: "POST", body: ask, credentials: "same-origin" });
      const data = await presign.json();
      if (!presign.ok) throw new Error(data.error || "Could not start the upload");

      const upload = new FormData();
      Object.entries(data.upload.fields).forEach(([key, value]) => upload.append(key, value));
      upload.append("file", file);
      const sent = await fetch(data.upload.url, { method: "POST", body: upload });
      if (!sent.ok) throw new Error("S3 refused the file (too large or wrong type?)");
      status.textContent = "Uploaded.";
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
    }
  });
})();
