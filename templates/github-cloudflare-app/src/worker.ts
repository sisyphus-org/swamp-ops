export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/health") {
      return Response.json({ status: "ok" });
    }

    if (url.pathname === "/api/version") {
      return Response.json({
        name: "__REPOSITORY__",
        version: "0.1.1",
      });
    }

    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Env>;