import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getDoc, getDocSlugs } from "@/lib/docs";
import {
  canonicalUrl,
  DEFAULT_DESCRIPTION,
  SITE_NAME,
  SOCIAL_IMAGE_URL,
} from "@/lib/seo";
import { CodeCopy } from "@/components/code-copy";

export const dynamicParams = false;

export function generateStaticParams() {
  return getDocSlugs().map((slug) => ({ slug }));
}

interface DocPageProps {
  params: Promise<{ slug?: string[] }>;
}

export async function generateMetadata(props: DocPageProps): Promise<Metadata> {
  const { slug = [] } = await props.params;
  const doc = await getDoc(slug);
  const path = slug.length ? `/docs/${slug.join("/")}/` : "/docs/";
  const title = doc ? doc.title : "WebSkrap Docs";
  const description = doc?.description ?? DEFAULT_DESCRIPTION;

  return {
    title,
    description,
    alternates: {
      canonical: canonicalUrl(path),
    },
    openGraph: {
      type: "article",
      url: canonicalUrl(path),
      title: `${title} | ${SITE_NAME}`,
      description,
      siteName: SITE_NAME,
      images: [
        {
          url: SOCIAL_IMAGE_URL,
          width: 642,
          height: 686,
          alt: "WebSkrap logo",
        },
      ],
    },
    twitter: {
      card: "summary",
      title: `${title} | ${SITE_NAME}`,
      description,
      images: [SOCIAL_IMAGE_URL],
    },
  };
}

export default async function DocPage(props: DocPageProps) {
  const { slug = [] } = await props.params;
  const doc = await getDoc(slug);
  if (!doc) notFound();
  const path = slug.length ? `/docs/${slug.join("/")}/` : "/docs/";
  const articleSchema = {
    "@context": "https://schema.org",
    "@type": "TechArticle",
    headline: doc.title,
    description: doc.description ?? DEFAULT_DESCRIPTION,
    url: canonicalUrl(path),
    about: ["Python web scraping", "web crawling", "browser automation", "Playwright"],
    author: {
      "@type": "Person",
      name: "Gaya KACI",
      url: "https://github.com/kacigaya",
    },
  };

  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(articleSchema) }}
      />
      <article
        className="prose prose-neutral max-w-none dark:prose-invert prose-headings:scroll-mt-24 prose-headings:text-balance prose-p:text-pretty prose-pre:rounded-2xl prose-pre:border prose-pre:bg-card prose-pre:p-6 prose-a:text-primary"
        dangerouslySetInnerHTML={{ __html: doc.html }}
      />
      <CodeCopy />
    </>
  );
}
