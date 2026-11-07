"use strict";

// 업로드 제한, 예약 가능 기간 등은 서버의 /api/config 값으로 덮어쓴다(서버가 기준).
const config = {
  max_upload_bytes: 5 * 1024 * 1024,
  max_import_bytes: 10 * 1024 * 1024,
  allowed_extensions: [".jpg", ".jpeg", ".png", ".gif", ".webp"],
  dangerous_extensions: [],
  max_advance_days: 90,
  pending_ttl_minutes: 15,
};
const STATUS_LABELS = { PENDING: "결제 대기", PAID: "예약 확정", CANCELLED: "취소됨" };

const $ = (selector, root = document) => root.querySelector(selector);
const won = (n) => `${Number(n).toLocaleString("ko-KR")}원`;
const km = (n) => `${Number(n).toLocaleString("ko-KR")} km`;

const state = { items: [], payingReservation: null };

/** 사용자 입력을 innerHTML 없이 안전하게 DOM으로 만든다. */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) el.append(child);
  }
  return el;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch { /* JSON이 아닌 응답 */ }
  if (!res.ok) throw new Error(data?.error || `요청에 실패했습니다. (${res.status})`);
  return data;
}

const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

let toastTimer;
function toast(message, type = "ok") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show ${type === "error" ? "error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function dateString(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ 탭 */

const tabLoaders = {
  items: loadItems,
  reserve: () => state.items.length || loadItems(),
  history: loadHistory,
  vehicles: loadVehicles,
  records: loadRecords,
};

function showTab(name) {
  if (!(name in tabLoaders)) name = "items";
  document.querySelectorAll(".tab").forEach((tab) => tab.setAttribute("aria-selected", tab.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((panel) => { panel.hidden = panel.id !== `tab-${name}`; });
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  tabLoaders[name]();
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => showTab(tab.dataset.tab)));

/* ------------------------------------------------------------ 정비 항목 */

async function loadItems() {
  try {
    const { items } = await api("/api/items");
    state.items = items;
    renderItemsTable();
    renderReserveItems();
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderItemsTable() {
  const keyword = $("#item-search").value.trim().toLowerCase();
  const rows = state.items.filter((item) =>
    `${item.part_name} ${item.job_name}`.toLowerCase().includes(keyword));
  const body = $("#items-body");
  body.replaceChildren(...(rows.length ? rows.map((item) => h("tr", {},
    h("td", { class: "part" }, item.part_name),
    h("td", {}, item.job_name),
    h("td", { class: "num" }, won(item.price)),
    h("td", { class: "num" }, h("span", { class: "km-chip" }, `${km(item.interval_km)}마다`)),
    h("td", { class: "num" }, h("button", {
      class: "btn small", type: "button", onclick: () => addToReservation(item.id),
    }, "예약 담기")),
  )) : [h("tr", {}, h("td", { colspan: 5, class: "empty" }, "검색 결과가 없습니다."))]));
}

$("#item-search").addEventListener("input", renderItemsTable);

function addToReservation(itemId) {
  const checkbox = $(`#reserve-items input[value="${itemId}"]`);
  if (checkbox) checkbox.checked = true;
  updateSummary();
  showTab("reserve");
  toast("예약 항목에 추가했습니다.");
}

/* ---------------------------------------------------------------- 예약 */

const reserveForm = $("#reserve-form");

function renderReserveItems() {
  const container = $("#reserve-items");
  const checked = new Set([...container.querySelectorAll("input:checked")].map((i) => i.value));
  container.replaceChildren(...state.items.map((item) => h("label", { class: "check-item" },
    h("input", { type: "checkbox", name: "item_ids", value: item.id, checked: checked.has(String(item.id)) }),
    h("div", {},
      h("div", { class: "name" }, item.part_name),
      h("div", { class: "meta" }, `${item.job_name} · ${won(item.price)}`)),
  )));
  updateSummary();
}

function selectedItems() {
  const ids = new Set([...reserveForm.querySelectorAll("input[name=item_ids]:checked")].map((i) => Number(i.value)));
  return state.items.filter((item) => ids.has(item.id));
}

function updateSummary() {
  const items = selectedItems();
  $("#summary-list").replaceChildren(...(items.length
    ? items.map((item) => h("li", {}, h("span", {}, item.part_name), h("span", {}, won(item.price))))
    : [h("li", { class: "muted" }, "선택한 항목이 없습니다.")]));
  $("#summary-total").textContent = won(items.reduce((sum, item) => sum + item.price, 0));
}

$("#reserve-items").addEventListener("change", updateSummary);

async function loadSlots(day) {
  const list = $("#slot-list");
  if (!day) {
    list.replaceChildren(h("span", { class: "muted" }, "예약일을 먼저 선택하세요."));
    return;
  }
  try {
    const { slots } = await api(`/api/slots?date=${encodeURIComponent(day)}`);
    list.replaceChildren(...slots.map((slot) => h("label", { class: "slot" },
      h("input", { type: "radio", name: "reserve_time", value: slot.time, disabled: !slot.available }),
      h("span", {}, slot.time))));
    if (!slots.some((slot) => slot.available)) {
      list.append(h("span", { class: "muted" }, "예약 가능한 시간이 없습니다."));
    }
  } catch (err) {
    list.replaceChildren(h("span", { class: "muted" }, err.message));
  }
}

const dateInput = reserveForm.elements.reserve_date;
dateInput.addEventListener("change", () => loadSlots(dateInput.value));

reserveForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = reserveForm.elements;
  const payload = {
    customer_name: form.customer_name.value.trim(),
    phone: form.phone.value.trim(),
    car_number: form.car_number.value.trim(),
    reserve_date: form.reserve_date.value,
    reserve_time: reserveForm.querySelector("input[name=reserve_time]:checked")?.value || "",
    item_ids: selectedItems().map((item) => item.id),
    memo: form.memo.value.trim(),
  };

  const missing = [
    [!payload.customer_name, "이름을 입력해 주세요."],
    [!payload.phone, "연락처를 입력해 주세요."],
    [!payload.car_number, "차량번호를 입력해 주세요."],
    [!payload.reserve_date, "예약일을 선택해 주세요."],
    [!payload.reserve_time, "예약 시간을 선택해 주세요."],
    [!payload.item_ids.length, "정비 항목을 하나 이상 선택해 주세요."],
  ].find(([invalid]) => invalid);
  if (missing) return toast(missing[1], "error");

  const button = reserveForm.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const { reservation } = await postJson("/api/reservations", payload);
    reserveForm.reset();
    loadSlots("");
    updateSummary();
    toast(`예약 #${reservation.id}이 접수되었습니다. 결제를 진행해 주세요.`);
    openPayment(reservation);
  } catch (err) {
    toast(err.message, "error");
    if (payload.reserve_date) loadSlots(payload.reserve_date);
  } finally {
    button.disabled = false;
  }
});

/* ------------------------------------------------------------ 결제 */

const payDialog = $("#pay-dialog");
const payForm = $("#pay-form");

function openPayment(reservation) {
  state.payingReservation = reservation;
  payForm.reset();
  toggleMethodFields();
  $("#pay-error").hidden = true;
  $("#pay-step").hidden = false;
  $("#pay-result").hidden = true;
  $("#pay-summary").replaceChildren(
    h("div", {}, h("strong", {}, `예약 #${reservation.id}`), ` · ${reservation.reserve_date} ${reservation.reserve_time}`),
    h("div", { class: "muted" }, reservation.items.map((item) => item.part_name).join(", ")),
    h("div", { class: "total" }, h("span", {}, "결제 금액"), h("span", {}, won(reservation.total_price))),
  );
  payDialog.showModal();
}

function toggleMethodFields() {
  const isCard = payForm.elements.method.value === "card";
  $("#card-fields").hidden = !isCard;
  $("#transfer-fields").hidden = isCard;
}

payForm.addEventListener("change", (event) => {
  if (event.target.name === "method") toggleMethodFields();
});

payForm.elements.card_number.addEventListener("input", (event) => {
  const digits = event.target.value.replace(/\D/g, "").slice(0, 16);
  event.target.value = digits.replace(/(\d{4})(?=\d)/g, "$1 ");
});
payForm.elements.expiry.addEventListener("input", (event) => {
  const digits = event.target.value.replace(/\D/g, "").slice(0, 4);
  event.target.value = digits.length > 2 ? `${digits.slice(0, 2)}/${digits.slice(2)}` : digits;
});
payForm.elements.cvc.addEventListener("input", (event) => {
  event.target.value = event.target.value.replace(/\D/g, "").slice(0, 3);
});

payDialog.querySelectorAll("[data-close]").forEach((btn) => btn.addEventListener("click", () => payDialog.close()));
payDialog.addEventListener("close", () => {
  if (!$("#tab-history").hidden) loadHistory();
});

payForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = payForm.elements;
  const reservation = state.payingReservation;
  const payload = form.method.value === "card"
    ? { method: "card", card_number: form.card_number.value, expiry: form.expiry.value, cvc: form.cvc.value }
    : { method: "transfer", depositor: form.depositor.value.trim() };

  const button = $("#pay-submit");
  const errorBox = $("#pay-error");
  button.disabled = true;
  button.textContent = "결제 승인 중…";
  errorBox.hidden = true;
  try {
    await new Promise((resolve) => setTimeout(resolve, 700)); // 결제 대기 연출
    const { reservation: paid } = await postJson(`/api/reservations/${reservation.id}/pay`, payload);
    $("#pay-result-info").replaceChildren(
      ...[["예약 번호", `#${paid.id}`], ["예약 일시", `${paid.reserve_date} ${paid.reserve_time}`],
          ["결제 수단", paid.payment_method], ["결제 금액", won(paid.total_price)],
          ["승인 번호", paid.transaction_id]]
        .flatMap(([label, value]) => [h("dt", {}, label), h("dd", {}, value)]),
    );
    $("#pay-step").hidden = true;
    $("#pay-result").hidden = false;
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "결제하기";
  }
});

/* ------------------------------------------------------------ 예약 내역 */

async function loadHistory() {
  const list = $("#history-list");
  try {
    const { reservations } = await api("/api/reservations");
    list.replaceChildren(...(reservations.length
      ? reservations.map(renderReservation)
      : [h("div", { class: "card empty" }, "예약 내역이 없습니다.")]));
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderReservation(r) {
  const thumb = r.vehicle_photo
    ? h("img", { class: "thumb", src: `/uploads/${encodeURIComponent(r.vehicle_photo)}`, alt: `${r.car_number} 차량 사진` })
    : h("div", { class: "thumb" }, "사진 없음");

  const actions = h("div", { class: "actions" });
  if (r.status === "PENDING") {
    actions.append(h("button", { class: "btn small primary", type: "button", onclick: () => openPayment(r) }, "결제하기"));
  }
  if (r.status !== "CANCELLED") {
    actions.append(h("button", { class: "btn small danger", type: "button", onclick: () => cancelReservation(r) },
      r.status === "PAID" ? "취소 (환불)" : "취소"));
  }

  return h("article", { class: "card reservation" },
    thumb,
    h("div", {},
      h("div", { class: "reservation-title" },
        `#${r.id} · ${r.reserve_date} ${r.reserve_time}`,
        h("span", { class: `badge ${r.status}` }, STATUS_LABELS[r.status] ?? r.status)),
      h("div", { class: "reservation-meta" },
        `${r.customer_name} · ${r.phone} · ${r.car_number}${r.vehicle_model ? ` (${r.vehicle_model})` : ""}`),
      h("div", { class: "reservation-items" }, r.items.map((item) => h("span", {}, item.part_name))),
      r.memo && h("div", { class: "reservation-pay" }, `요청사항: ${r.memo}`),
      r.payment_deadline && h("div", { class: "reservation-pay deadline" },
        `${r.payment_deadline.slice(11, 16)}까지 결제하지 않으면 자동 취소됩니다.`),
      r.status === "PAID" && h("div", { class: "reservation-pay" },
        `${r.payment_method} · 승인번호 ${r.transaction_id} · ${r.paid_at.replace("T", " ")}`)),
    h("div", { class: "reservation-side" }, h("strong", {}, won(r.total_price)), actions),
  );
}

async function cancelReservation(r) {
  const message = r.status === "PAID"
    ? `예약 #${r.id}을 취소하고 ${won(r.total_price)}을 환불하시겠습니까?`
    : `예약 #${r.id}을 취소하시겠습니까?`;
  if (!confirm(message)) return;
  try {
    const result = await postJson(`/api/reservations/${r.id}/cancel`, {});
    toast(result.message);
    loadHistory();
  } catch (err) {
    toast(err.message, "error");
  }
}

$("#history-refresh").addEventListener("click", loadHistory);

/* ------------------------------------------------------------ 차량 프로필 */

const vehicleForm = $("#vehicle-form");
const photoInput = $("#photo-input");
const preview = $("#photo-preview");
const dropzone = $("#dropzone");
let previewUrl = null;

function setPreview(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = file ? URL.createObjectURL(file) : null;
  preview.hidden = !file;
  $("#dropzone-text").hidden = Boolean(file);
  if (file) preview.src = previewUrl;
  else preview.removeAttribute("src");
}

/** 서버에서도 동일하게 검사하지만, 업로드 전에 먼저 알려준다. */
function validatePhoto(file) {
  const suffixes = file.name.toLowerCase().split(".").slice(1).map((s) => `.${s}`);
  const ext = suffixes.at(-1) ?? "";
  const maxMb = config.max_upload_bytes / 1024 / 1024;
  if (suffixes.some((s) => config.dangerous_extensions.includes(s))) {
    return "위험한 확장자입니다. 실행 파일이나 스크립트는 업로드할 수 없습니다.";
  }
  if (!config.allowed_extensions.includes(ext)) {
    return `허용되지 않는 파일 형식입니다. (${config.allowed_extensions.join(", ")})`;
  }
  if (file.size > config.max_upload_bytes) {
    return `파일 크기는 최대 ${maxMb}MB까지 업로드할 수 있습니다. (현재 ${(file.size / 1024 / 1024).toFixed(1)}MB)`;
  }
  return null;
}

photoInput.addEventListener("change", () => {
  const file = photoInput.files[0];
  if (!file) return setPreview(null);
  const error = validatePhoto(file);
  if (error) {
    photoInput.value = "";
    setPreview(null);
    return toast(error, "error");
  }
  setPreview(file);
});

["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, () => dropzone.classList.add("dragover")));
["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, () => dropzone.classList.remove("dragover")));

vehicleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = vehicleForm.elements;
  if (!form.car_number.value.trim()) return toast("차량번호를 입력해 주세요.", "error");
  if (!form.model.value.trim()) return toast("차종을 입력해 주세요.", "error");

  const button = vehicleForm.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const { vehicle, created } = await api("/api/vehicles", { method: "POST", body: new FormData(vehicleForm) });
    toast(created ? `${vehicle.car_number} 차량이 등록되었습니다.` : `${vehicle.car_number} 차량 정보가 수정되었습니다.`);
    vehicleForm.reset();
    setPreview(null);
    loadVehicles();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
});

async function loadVehicles() {
  const grid = $("#vehicle-grid");
  try {
    const { vehicles } = await api("/api/vehicles");
    grid.replaceChildren(...(vehicles.length ? vehicles.map((v) => h("article", {
      class: "card vehicle",
      title: "클릭하면 수정할 수 있습니다",
      onclick: () => editVehicle(v),
    },
      v.photo
        ? h("img", { src: `/uploads/${encodeURIComponent(v.photo)}`, alt: `${v.car_number} 차량 사진`, loading: "lazy" })
        : h("div", { class: "vehicle-placeholder" }, "차량 사진 없음"),
      h("div", { class: "vehicle-body" },
        h("strong", {}, v.car_number),
        h("div", {}, `${v.model} · ${km(v.mileage)}`),
        h("div", {}, v.photo ? `사진: ${v.photo}` : "차량 기록에서 가져온 프로필")),
    )) : [h("div", { class: "card empty" }, "등록된 차량이 없습니다. 왼쪽에서 차량 사진을 등록해 보세요.")]));
  } catch (err) {
    toast(err.message, "error");
  }
}

function editVehicle(v) {
  const form = vehicleForm.elements;
  form.car_number.value = v.car_number;
  form.model.value = v.model;
  form.mileage.value = v.mileage;
  setPreview(null);
  form.car_number.focus();
  toast("사진을 선택하지 않으면 기존 사진이 유지됩니다.");
}

/* --------------------------------------------------------- 차량 기록 가져오기 */

const importForm = $("#michael-import-form");
const importFile = $("#michael-file");
const recordsCarFilter = $("#records-car-filter");
const recordsTypeFilter = $("#records-type-filter");

importForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = importFile.files[0];
  if (!file) return toast("마이클에서 내보낸 .xls 파일을 선택해 주세요.", "error");
  if (!file.name.toLowerCase().endsWith(".xls")) return toast("Excel 97-2003 형식의 .xls 파일만 가져올 수 있습니다.", "error");
  if (file.size > config.max_import_bytes) return toast("가져오기 파일은 최대 10MB까지 업로드할 수 있습니다.", "error");

  const button = importForm.querySelector("button[type=submit]");
  const result = $("#import-result");
  button.disabled = true;
  result.textContent = "";
  try {
    const { vehicle, imported_count, duplicate_count } = await api("/api/import/michael", {
      method: "POST",
      body: new FormData(importForm),
    });
    result.textContent = `${vehicle.car_number} 기록 ${imported_count.toLocaleString("ko-KR")}건을 추가했습니다. 중복 ${duplicate_count.toLocaleString("ko-KR")}건은 건너뛰었습니다.`;
    importForm.reset();
    await loadVehicles();
    await loadRecords(vehicle.car_number);
    toast("마이클 차량 기록을 가져왔습니다.");
  } catch (err) {
    result.textContent = err.message;
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("#records-refresh").addEventListener("click", () => loadRecords());
recordsCarFilter.addEventListener("change", () => loadRecords());
recordsTypeFilter.addEventListener("change", () => loadRecords());

async function loadRecords(selectCar = null) {
  const body = $("#records-body");
  try {
    const { vehicles } = await api("/api/vehicles");
    const selectedCar = selectCar ?? recordsCarFilter.value;
    const options = [h("option", { value: "" }, "모든 차량")];
    for (const vehicle of vehicles) {
      options.push(h("option", { value: vehicle.car_number },
        `${vehicle.car_number} · ${vehicle.model}`));
    }
    recordsCarFilter.replaceChildren(...options);
    recordsCarFilter.value = vehicles.some((vehicle) => vehicle.car_number === selectedCar) ? selectedCar : "";

    const params = new URLSearchParams();
    if (recordsCarFilter.value) params.set("car_number", recordsCarFilter.value);
    if (recordsTypeFilter.value) params.set("type", recordsTypeFilter.value);
    const queryString = params.toString();
    const query = queryString ? `?${queryString}` : "";
    const { records } = await api(`/api/records${query}`);
    const expense = records.filter((record) => record.record_type !== "주행")
      .reduce((sum, record) => sum + record.amount, 0);
    const latestMileage = records.reduce((latest, record) => Math.max(latest, record.mileage || 0), 0);
    $("#records-summary").replaceChildren(
      h("article", { class: "card record-stat" }, h("span", {}, "기록 수"), h("strong", {}, `${records.length.toLocaleString("ko-KR")}건`)),
      h("article", { class: "card record-stat" }, h("span", {}, "지출 합계"), h("strong", {}, won(expense))),
      h("article", { class: "card record-stat" }, h("span", {}, "누적 주행거리"), h("strong", {}, latestMileage ? km(latestMileage) : "—")),
    );
    body.replaceChildren(...(records.length ? records.map((record) => h("tr", {},
      h("td", {}, record.record_date),
      h("td", {}, record.car_number),
      h("td", {}, h("span", { class: `badge record-type ${record.record_type}` }, record.record_type)),
      h("td", { class: "part" }, record.detail),
      h("td", { class: "num" }, record.record_type === "주행" ? "—" : won(record.amount)),
      h("td", { class: "num" }, record.mileage ? km(record.mileage) : "—"),
      h("td", {}, record.place || "—"),
      h("td", { class: "record-memo" }, record.memo || "—"),
    )) : [h("tr", {}, h("td", { colspan: 8, class: "empty" }, "표시할 차량 기록이 없습니다. 마이클 리포트를 가져와 보세요."))]));
  } catch (err) {
    toast(err.message, "error");
    body.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "empty" }, "차량 기록을 불러오지 못했습니다.")));
  }
}

/* ---------------------------------------------------------------- 시작 */

async function start() {
  try {
    Object.assign(config, await api("/api/config"));
  } catch (err) {
    toast(`설정을 불러오지 못해 기본값을 사용합니다: ${err.message}`, "error");
  }
  dateInput.min = dateString();
  dateInput.max = dateString(config.max_advance_days);
  showTab(location.hash.slice(1));
}

start();
