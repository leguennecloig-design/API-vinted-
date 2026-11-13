// Service worker minimal : permet d'afficher les notifications d'alerte (obligatoire sur Android)
// et d'ouvrir l'annonce quand on touche la notification.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = e.notification.data && e.notification.data.url;
  if (url) e.waitUntil(self.clients.openWindow(url));
});
