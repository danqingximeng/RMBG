"use strict";

const $ = (id) => document.getElementById(id);
const isDesktop = () => window.matchMedia("(min-width: 901px)").matches;
let model = "";
let fileObj = null;
let origUrl = null,
  resUrl = null,
  fileName = "";
let cmpPos = 50; // 滑块分隔线位置（%）

/* ---------- 抽屉（窄屏） ---------- */
function setOpen(open) {
  document.body.classList.toggle("open", open);
}
$("sideToggle").onclick = () =>
  setOpen(!document.body.classList.contains("open"));
$("sideClose").onclick = () => setOpen(false);
$("backdrop").onclick = () => setOpen(false);
window
  .matchMedia("(min-width: 901px)")
  .addEventListener("change", (e) => setOpen(e.matches));

let swipeX = null,
  swipeY = null;
window.addEventListener(
  "touchstart",
  (e) => {
    const t = e.changedTouches[0];
    swipeX = t.clientX;
    swipeY = t.clientY;
  },
  { passive: true },
);
window.addEventListener(
  "touchmove",
  (e) => {
    if (swipeX === null || document.body.classList.contains("open")) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - swipeX;
    const dy = t.clientY - swipeY;
    if (swipeX < 32 && dx > 60 && Math.abs(dy) < Math.abs(dx)) {
      setOpen(true);
      swipeX = null;
    }
  },
  { passive: true },
);
window.addEventListener("touchend", () => {
  swipeX = swipeY = null;
});

/* ---------- 参数滑条的数值显示 ---------- */
const setVal = (id, v) => ($(id + "_val").textContent = v);
$("res").oninput = (e) => setVal("res", e.target.value);
$("sens").oninput = (e) => setVal("sens", (+e.target.value).toFixed(2));
$("blur").oninput = (e) => setVal("blur", e.target.value);
$("off").oninput = (e) => setVal("off", e.target.value);
setVal("res", 1024);
setVal("sens", "1.00");
setVal("blur", 0);
setVal("off", 0);

/* ---------- 模型 ---------- */
async function init() {
  try {
    const r = await fetch("/api/models");
    const data = await r.json();
    if (!r.ok) throw new Error(data.error?.message || "failed to load models");
    const sel = $("model");
    for (const m of data.data) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.id;
      sel.appendChild(opt);
    }
    const def = data.default || (data.data[0] && data.data[0].id);
    sel.value = def;
    model = def;
    sel.onchange = () => {
      model = sel.value;
    };
  } catch (err) {
    $("status").textContent = "模型列表加载失败: " + err.message;
    $("run").disabled = true;
  }
}
init();

function revoke(url) {
  if (url) URL.revokeObjectURL(url);
}

/* ---------- 显示 / 滑块对比（左原图右结果） ---------- */
function showImage() {
  $("drop").style.display = "none";
  $("bar").style.display = "flex";
  $("cmp").style.display = "block";
  const b = $("beforeImg");
  b.onload = fitCmp;
  b.src = origUrl;
  if (b.complete) fitCmp();
  const a = $("afterImg");
  if (resUrl) {
    a.src = resUrl;
    a.style.display = "block";
    $("divider").style.display = "block";
  } else {
    a.removeAttribute("src");
    a.style.display = "none";
    $("divider").style.display = "none";
  }
  $("download").disabled = !resUrl;
  $("run").disabled = false;
  applyView();
}

function applyView() {
  if (!origUrl) return;
  const b = $("beforeImg");
  const a = $("afterImg");
  if (resUrl) {
    b.style.clipPath = `inset(0 ${100 - cmpPos}% 0 0)`;
    a.style.clipPath = `inset(0 0 0 ${cmpPos}%)`;
  } else {
    b.style.clipPath = "";
    a.style.clipPath = "";
  }
  $("divider").style.left = cmpPos + "%";
}

function fitCmp() {
  const img = $("beforeImg");
  if (!img.naturalWidth) return;
  const box = $("viewer").getBoundingClientRect();
  const availW = box.width - 48,
    availH = box.height - 48;
  let w = img.naturalWidth,
    h = img.naturalHeight;
  const ratio = w / h;
  if (w > availW) {
    w = availW;
    h = w / ratio;
  }
  if (h > availH) {
    h = availH;
    w = h * ratio;
  }
  $("cmp").style.width = w + "px";
  $("cmp").style.height = h + "px";
  applyView();
}

const cmp = $("cmp");
let dragging = false;
cmp.addEventListener("pointerdown", (e) => {
  dragging = true;
  cmp.setPointerCapture(e.pointerId);
  moveSlider(e);
});
cmp.addEventListener("pointermove", (e) => {
  if (dragging) moveSlider(e);
});
cmp.addEventListener("pointerup", () => {
  dragging = false;
});
cmp.addEventListener("pointercancel", () => {
  dragging = false;
});
function moveSlider(e) {
  const rect = cmp.getBoundingClientRect();
  cmpPos = Math.min(
    100,
    Math.max(0, ((e.clientX - rect.left) / rect.width) * 100),
  );
  applyView();
  e.preventDefault();
}
window.addEventListener("resize", () => {
  if (origUrl) fitCmp();
});

/* ---------- 文件选择 ---------- */
function pickFile(f) {
  if (!f) return;
  fileObj = f;
  revoke(origUrl);
  revoke(resUrl);
  origUrl = resUrl = null;
  fileName = f.name;
  origUrl = URL.createObjectURL(f);
  $("fname").textContent = fileName;
  $("time").textContent = "";
  $("status").textContent = "";
  showImage();
}

$("drop").onclick = () => $("file").click();
$("drop").ondragover = (e) => {
  e.preventDefault();
  $("drop").classList.add("drag");
};
$("drop").ondragleave = () => $("drop").classList.remove("drag");
$("drop").ondrop = (e) => {
  e.preventDefault();
  $("drop").classList.remove("drag");
  pickFile(e.dataTransfer.files[0]);
};
$("file").onchange = (e) => pickFile(e.target.files[0]);
$("change").onclick = () => $("file").click();

/* ---------- 下载 ---------- */
$("download").onclick = () => {
  if (!resUrl) return;
  const a = document.createElement("a");
  a.href = resUrl;
  a.download = fileName.replace(/\.[^.]+$/, "") + ".png";
  a.click();
};

/* ---------- 去背景 ---------- */
$("run").onclick = async () => {
  if (!origUrl) return;
  if (!isDesktop()) setOpen(false);
  const form = new FormData();
  form.append("file", fileObj);
  form.append("model", model);
  form.append("process_res", $("res").value);
  form.append("sensitivity", $("sens").value);
  form.append("mask_blur", $("blur").value);
  form.append("mask_offset", $("off").value);
  form.append("refine", $("refine").checked);
  $("run").disabled = true;
  $("busy").style.display = "flex";
  $("status").textContent = "";
  try {
    const r = await fetch("/api/rmbg", { method: "POST", body: form });
    const json = await r.json();
    if (!r.ok) throw new Error(json.error?.message || "remove-bg failed");
    const item = json.data[0];
    const elapsed = json.usage
      ? (json.usage.elapsed_ms / 1000).toFixed(1)
      : null;
    revoke(resUrl);
    resUrl = URL.createObjectURL(
      new Blob([base64ToBytes(item.b64_json)], { type: "image/png" }),
    );
    showImage();
    $("time").textContent = elapsed
      ? `用时 ${elapsed}s · ${item.width}×${item.height} · ${json.model}`
      : "";
    $("status").textContent = elapsed ? `完成，用时 ${elapsed} 秒` : "完成";
  } catch (err) {
    $("status").textContent = "出错: " + err.message;
    $("run").disabled = false;
  } finally {
    $("busy").style.display = "none";
  }
};

function base64ToBytes(b64) {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

setOpen(isDesktop());
