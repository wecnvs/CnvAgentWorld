// remote.html 진입 — 별도 창에서 원격 세션 그리드를 그린다.
// URL ?targets=A,B,C (타깃 별칭, comma) → 각 세션 타일을 띄운다. 라벨은 /api/apps의 targets에서 조회.
import { openRemotePanel, setStandalone } from "./remote-panel.js?v=20260702-08";

setStandalone(true);

(async () => {
  const params = new URLSearchParams(location.search);
  const names = (params.get("targets") || "")
    .split(",").map((s) => decodeURIComponent(s.trim())).filter(Boolean);
  const empty = document.getElementById("rpw-empty");
  if (!names.length) {
    if (empty) empty.textContent = "대상 세션이 지정되지 않았습니다 (?targets=...).";
    return;
  }
  // 라벨 조회(없어도 별칭으로 진행)
  let labels = {};
  try {
    const d = await (await fetch("/api/apps")).json();
    (d.targets || []).forEach((t) => { labels[t.name] = t.label || t.name; });
  } catch (_) {}
  if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
  names.forEach((n) => openRemotePanel(n, labels[n] || n));
  document.title = names.length > 1
    ? `원격 ${names.length}개 세션 — CnvAgentWorld`
    : `원격: ${labels[names[0]] || names[0]}`;
})();
