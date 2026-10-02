"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV } from "@/lib/docs-nav";
import { cn } from "@/lib/utils";

// The export uses trailingSlash, so usePathname returns "/docs/x/" while the nav
// declares "/docs/x". Compare both without the trailing slash.
function samePath(left: string, right: string) {
  const trim = (value: string) => (value.length > 1 ? value.replace(/\/$/, "") : value);
  return trim(left) === trim(right);
}

export function DocsSidebar() {
  const pathname = usePathname();

  return (
    <nav aria-label="Documentation" className="flex flex-col gap-6 text-sm">
      {NAV.map((section, i) => (
        <div key={section.title ?? i} className="flex flex-col gap-1">
          {section.title && (
            <p className="mb-1 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {section.title}
            </p>
          )}
          {section.items.map((item) => {
            const active = samePath(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "rounded-md px-2 py-1.5 transition-colors",
                  active
                    ? "bg-accent font-medium text-brand"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                )}
              >
                {item.title}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
