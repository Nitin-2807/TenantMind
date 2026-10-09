"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/cn";
import {
  ChatIcon,
  ChartIcon,
  DashboardIcon,
  DocumentsIcon,
  HistoryIcon,
  SettingsIcon,
  UsersIcon,
} from "@/components/icons";
import { navForRole, type NavItem } from "@/components/layout/nav-items";
import type { UserRole } from "@/lib/types";

const iconByKey: Record<NavItem["iconKey"], (props: { className?: string }) => React.JSX.Element> = {
  dashboard: DashboardIcon,
  documents: DocumentsIcon,
  assistant: ChatIcon,
  history: HistoryIcon,
  users: UsersIcon,
  usage: ChartIcon,
  settings: SettingsIcon,
};

function NavLinks({ role, onNavigate }: { role: UserRole; onNavigate?: () => void }) {
  const pathname = usePathname();
  const items = navForRole(role);

  return (
    <nav aria-label="Main navigation" className="flex flex-1 flex-col gap-1 px-3">
      {items.map((item) => {
        const Icon = iconByKey[item.iconKey];
        const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              active
                ? "bg-primary/10 text-primary"
                : "text-muted hover:bg-surface hover:text-foreground",
            )}
          >
            <Icon className="h-5 w-5 shrink-0" />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

export function SidebarBrand() {
  return (
    <div className="flex h-14 items-center gap-2 border-b border-border px-5">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-sm font-bold text-primary-foreground">
        T
      </span>
      <span className="text-base font-semibold tracking-tight text-foreground">TenantMind</span>
    </div>
  );
}

export function SidebarContent({
  role,
  onNavigate,
}: {
  role: UserRole;
  onNavigate?: () => void;
}) {
  return (
    <div className="flex h-full flex-col">
      <SidebarBrand />
      <div className="flex-1 overflow-y-auto py-4">
        <NavLinks role={role} onNavigate={onNavigate} />
      </div>
    </div>
  );
}