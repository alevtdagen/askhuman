import { api, el, requestCard } from "./shared.js";
const requestId = location.pathname.split("/").pop();
const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
const root = document.getElementById("reply-content");
async function show() {
  const request = await api(`/api/replies/${requestId}`, token);
  root.replaceChildren(
    requestCard(
      request,
      async (body) => {
        await api(`/api/replies/${requestId}`, token, "POST", body);
        await show();
      },
      true,
    ),
  );
}
if (!token)
  root.replaceChildren(
    el(
      "p",
      { className: "error" },
      "Open the complete link from your notification to view this request.",
    ),
  );
else
  show().catch((error) =>
    root.replaceChildren(el("p", { className: "error", role: "alert" }, error.message)),
  );
