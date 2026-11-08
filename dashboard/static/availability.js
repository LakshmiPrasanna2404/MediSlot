// Availability editor: add and remove pattern rows. Blank rows are ignored on save.
(function () {
  const table = document.querySelector("#pattern-table tbody");
  if (!table) return;

  table.addEventListener("click", function (event) {
    const button = event.target.closest(".remove-row");
    if (!button) return;
    const rows = table.querySelectorAll(".pattern-row");
    const row = button.closest("tr");
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
