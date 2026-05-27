// static/sw.js

self.addEventListener('install', (event) => {
  self.skipWaiting();
  console.log('Service Worker installed');
});

self.addEventListener('activate', (event) => {
  console.log('Service Worker activated');
});

self.addEventListener('fetch', (event) => {
  // This empty fetch handler is required to pass PWA criteria
  // We just let the request go through to the network normally
});