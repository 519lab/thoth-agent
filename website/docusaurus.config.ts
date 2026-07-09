import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Thoth Agent',
  tagline: 'The self-improving agent with a cognitive memory substrate',
  favicon: 'img/favicon.svg',

  url: 'https://thoth.519lab.com',
  baseUrl: '/docs/',

  organizationName: '519lab',
  projectName: 'thoth-agent',

  onBrokenLinks: 'warn',

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    // English-only until the docs are substantially translated — building all
    // locales shipped ~650 untranslated English pages under /ko/ (duplicate
    // content + "pick language → get English" UX). Re-add 'ko' here (and the
    // localeDropdown below) once website/i18n/ is populated; the localeConfigs
    // are kept ready for that.
    locales: ['en'],
    localeConfigs: {
      // htmlLang is region-qualified so og:locale (language_TERRITORY) and the
      // hreflang alternates are valid; the locale *key* still drives the URL
      // path (/docs/ko/), so URLs are unchanged.
      en: {
        label: 'English',
        htmlLang: 'en-US',
      },
      ko: {
        label: '한국어',
        htmlLang: 'ko-KR',
      },
    },
  },

  headTags: [
    {
      tagName: 'script',
      attributes: { type: 'application/ld+json' },
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'SoftwareApplication',
        name: 'Thoth',
        applicationCategory: 'DeveloperApplication',
        operatingSystem: 'Linux, macOS, Windows (WSL2)',
        offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
        url: 'https://thoth.519lab.com/',
        downloadUrl: 'https://github.com/519lab/thoth-agent',
        license: 'https://opensource.org/licenses/MIT',
        description:
          'A self-improving, self-hostable AI agent with a cognitive memory substrate — it builds skills from experience and remembers across sessions.',
      }),
    },
  ],

  themes: [
    '@docusaurus/theme-mermaid',
    [
      require.resolve('@easyops-cn/docusaurus-search-local'),
      /** @type {import("@easyops-cn/docusaurus-search-local").PluginOptions} */
      ({
        hashed: true,
        // English-only site → only the English tokenizer is needed (no
        // @node-rs/jieba native dependency).
        language: ['en'],
        indexBlog: false,
        docsRouteBasePath: '/',
        // Disabled: appends ?_highlight=... to URLs (before the #anchor),
        // which makes copy/pasted doc links ugly. Ctrl+F on the page is fine.
        highlightSearchTermsOnTargetPage: false,
        // Exclude the auto-generated per-skill catalog pages from search.
        // There are hundreds of them and they dominate results for generic
        // terms, drowning out the real user-guide / reference docs.
        // The two human-written catalog indexes (reference/skills-catalog,
        // reference/optional-skills-catalog) remain indexed.
        //
        // Note: ignoreFiles matches `route` (baseUrl stripped, no leading
        // slash). With baseUrl '/docs/', `/docs/user-guide/skills/bundled/x`
        // becomes 'user-guide/skills/bundled/x'.
        ignoreFiles: [
          /^user-guide\/skills\/bundled\//,
          /^user-guide\/skills\/optional\//,
        ],
      }),
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',  // Docs at the root of /docs/
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/519lab/thoth-agent/edit/main/website/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/og-card.png',
    metadata: [
      {
        name: 'keywords',
        content:
          'AI agent, self-improving agent, autonomous agent, AI memory, cognitive substrate, pgvector, self-hostable, developer tool, open source',
      },
    ],
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    docs: {
      sidebar: {
        hideable: true,
        autoCollapseCategories: true,
      },
    },
    navbar: {
      title: 'Thoth Agent',
      logo: {
        alt: 'Thoth Agent',
        src: 'img/thoth-mark.svg',
        width: 30,
        height: 30,
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/skills',
          label: 'Skills',
          position: 'left',
        },
        // localeDropdown removed while the site is English-only — re-add when
        // translations land (see the i18n note above).
        {
          href: 'https://thoth.519lab.com',
          label: 'Home',
          position: 'right',
        },
        {
          href: 'https://github.com/519lab/thoth-agent',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            { label: 'Getting Started', to: '/getting-started/quickstart' },
            { label: 'User Guide', to: '/user-guide/cli' },
            { label: 'Developer Guide', to: '/developer-guide/architecture' },
            { label: 'Reference', to: '/reference/cli-commands' },
          ],
        },
        {
          title: 'Community',
          items: [
            { label: 'GitHub Discussions', href: 'https://github.com/519lab/thoth-agent/discussions' },
            { label: 'Skills Hub', href: 'https://agentskills.io' },
          ],
        },
        {
          title: 'More',
          items: [
            { label: 'GitHub', href: 'https://github.com/519lab/thoth-agent' },
            { label: 'Nous Research', href: 'https://nousresearch.com' },
          ],
        },
      ],
      copyright: `Built by <a href="https://github.com/519lab">519lab</a> · MIT-licensed fork of Hermes by <a href="https://nousresearch.com">Nous Research</a> · ${new Date().getFullYear()}`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'yaml', 'json', 'python', 'toml'],
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
