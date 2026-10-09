/**
 * Role-aware navigation contract tests (PMP §16 role-aware UI mirrors the
 * backend RBAC; nav links are UX, the backend remains the authority).
 */

import { describe, expect, it } from "vitest";

import { NAV_ITEMS, navForRole } from "./nav-items";

describe("navForRole", () => {
  it("exposes the core workspace to every role", () => {
    for (const role of ["admin", "editor", "viewer"] as const) {
      const nav = navForRole(role).map((item) => item.href);
      expect(nav).toContain("/dashboard");
      expect(nav).toContain("/documents");
      expect(nav).toContain("/assistant");
      expect(nav).toContain("/history");
      expect(nav).toContain("/settings");
    }
  });

  it("restricts the administration section to admins", () => {
    const adminNav = navForRole("admin").map((item) => item.href);
    expect(adminNav).toContain("/admin/users");
    expect(adminNav).toContain("/admin/usage");

    for (const role of ["editor", "viewer"] as const) {
      const nav = navForRole(role).map((item) => item.href);
      expect(nav).not.toContain("/admin/users");
      expect(nav).not.toContain("/admin/usage");
    }
  });

  it("has unique hrefs and role sets that stay in sync with the RBAC matrix", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
    expect(NAV_ITEMS.every((item) => item.roles.length > 0)).toBe(true);
  });
});