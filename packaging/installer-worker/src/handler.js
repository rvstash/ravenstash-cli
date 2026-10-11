const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "Cloudflare-CDN-Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
  "Content-Type": "text/plain; charset=utf-8",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Referrer-Policy": "no-referrer",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Robots-Tag": "noindex, nofollow",
};

export function handleRequest(request, installers) {
  const url = new URL(request.url);
  const installer = installers[url.pathname];
  if (
    url.protocol !== "https:" ||
    url.hostname !== "ravenstash.com" ||
    installer === undefined
  ) {
    return new Response("Not found\n", { status: 404 });
  }
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed\n", {
      status: 405,
      headers: { Allow: "GET, HEAD" },
    });
  }
  return new Response(request.method === "HEAD" ? null : installer, {
    status: 200,
    headers: SECURITY_HEADERS,
  });
}
