import type { MetadataRoute } from "next";
import { getDocSlugs } from "@/lib/docs";
import { SITE_URL } from "@/lib/seo";

export const dynamic = "force-static";

export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();
  const docs = getDocSlugs().map((slug) => {
    const path = slug.length ? `/docs/${slug.join("/")}/` : "/docs/";
    return {
      url: `${SITE_URL}${path}`,
      lastModified: now,
      changeFrequency: "weekly" as const,
      priority: slug.length ? 0.75 : 0.9,
    };
  });

  const routes = [
    {
      url: `${SITE_URL}/`,
      lastModified: now,
      changeFrequency: "weekly" as const,
      priority: 1,
    },
    {
      url: `${SITE_URL}/docs/benchmarks/`,
      lastModified: now,
      changeFrequency: "monthly" as const,
      priority: 0.6,
    },
    { url: `${SITE_URL}/privacy/`, lastModified: now, changeFrequency: "yearly" as const, priority: 0.3 },
    { url: `${SITE_URL}/cookies/`, lastModified: now, changeFrequency: "yearly" as const, priority: 0.3 },
  ];

  return [...routes, ...docs];
}
