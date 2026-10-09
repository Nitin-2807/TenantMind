import type { UserRole } from "@/lib/types";

export interface NavItem {
  href: string;
  label: string;
  /** Icon component key — resolved in the sidebar renderer. */
  iconKey: "dashboard" | "documents" | "assistant" | "history" | "users" | "usage" | "settings";
  /** Roles that may see the section. PMP §16 + §10 role-aware navigation. */
  roles: UserRole[];
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", iconKey: "dashboard", roles: ["admin", "editor", "viewer"] },
  { href: "/documents", label: "Documents", iconKey: "documents", roles: ["admin", "editor", "viewer"] },
  { href: "/assistant", label: "AI Assistant", iconKey: "assistant", roles: ["admin", "editor", "viewer"] },
  { href: "/history", label: "Query History", iconKey: "history", roles: ["admin", "editor", "viewer"] },
  { href: "/admin/users", label: "Administration", iconKey: "users", roles: ["admin"] },
  { href: "/admin/usage", label: "Usage", iconKey: "usage", roles: ["admin"] },
  { href: "/settings", label: "Settings", iconKey: "settings", roles: ["admin", "editor", "viewer"] },
];

/** The navigation a given role is allowed to see. */
export function navForRole(role: UserRole): NavItem[] {
  return NAV_ITEMS.filter((item) => item.roles.includes(role));
}