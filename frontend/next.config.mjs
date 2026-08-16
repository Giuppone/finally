/** @type {import('next').NextConfig} */
const nextConfig = {
  // PLAN.md §3: a static export served by FastAPI from the same origin. One port, one
  // container, no CORS. Nothing here may render on a server — there isn't one at runtime.
  output: "export",

  // The export has no Next image optimisation server behind it.
  images: { unoptimized: true },

  // Emit `out/index.html` rather than bare `out/index`, so StaticFiles(html=True) resolves
  // `/` without any rewrite rules on the Python side.
  trailingSlash: true,

  reactStrictMode: true,
};

export default nextConfig;
