// The case behind this file: `workshop_url` comes from the "request a mod" form (any signed-in
// user) and went unchecked into an `href` that an admin clicks. The access token lives in
// localStorage — so `javascript:` there is the path from "may write tickets" to "is admin".
import { describe, expect, it } from "vitest";
import { safeUrl } from "./url";

describe("safeUrl", () => {
  it("lets real web links through", () => {
    expect(safeUrl("https://steamcommunity.com/sharedfiles/filedetails/?id=123"))
      .toBe("https://steamcommunity.com/sharedfiles/filedetails/?id=123");
    expect(safeUrl("http://example.org/a?b=c#d")).toBe("http://example.org/a?b=c#d");
    expect(safeUrl("/join")).toBe("/join");   // relative, no scheme
  });

  it("drops everything else", () => {
    for (const bad of [
      "javascript:alert(document.cookie)",
      "JavaScript:alert(1)",
      "  javascript:alert(1)  ",
      "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
      "vbscript:msgbox(1)",
      "file:///etc/passwd",
      "//other-host/path",   // protocol-relative — leads out of the application
    ]) {
      expect(safeUrl(bad), bad).toBeUndefined();
    }
  });

  it("copes with empty and broken input", () => {
    expect(safeUrl("")).toBeUndefined();
    expect(safeUrl(null)).toBeUndefined();
    expect(safeUrl(undefined)).toBeUndefined();
    expect(safeUrl("not a link")).toBeUndefined();
  });
});
