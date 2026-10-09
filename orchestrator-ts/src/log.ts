// Same line format as the Python orchestrator: "<time> <LEVEL> render-worker <message>".
const write = (level: string) => (msg: string) =>
  console.log(`${new Date().toISOString().replace("T", " ").replace("Z", "")} ${level} render-worker ${msg}`);

export const log = { info: write("INFO"), warn: write("WARNING"), error: write("ERROR") };
