// Availability editor: add and remove pattern rows. Blank rows are ignored on save.
(function () {
  const table = document.querySelector("#pattern-table tbody");
  if (!table) return;

  table.addEventListener("click", function (event) {
    if (!event.target.classList.contains("remove-row")) return;
    const rows = table.querySelectorAll(".pattern-row");
    const row = event.target.closest("tr");
    if (rows.length > 1) {
      row.remove();
    } else {
      row.querySelectorAll("input").forEach((input) => (input.value = ""));
      row.querySelector("select[name=day_of_week]").value = "";
    }
  });

  document.getElementById("add-row").addEventListener("click", function () {
    const last = table.querySelector(".pattern-row:last-child");
    const copy = last.cloneNode(true);
    copy.querySelectorAll("input").forEach((input) => (input.value = ""));
    copy.querySelector("select[name=day_of_week]").value = "";
    table.appendChild(copy);
  });
})();
