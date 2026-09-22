// Small client-side enhancements.
// Sensitive API credentials are intentionally never placed here.

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("tr.clickable, .customer.clickable").forEach((item) => {
        item.setAttribute("title", "Open details");
    });
});
