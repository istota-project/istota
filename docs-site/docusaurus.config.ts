import type * as Preset from '@docusaurus/preset-classic';
import type {Config} from '@docusaurus/types';
import {themes as prismThemes} from 'prism-react-renderer';

// The markdown lives at the repository root in `docs/`, not inside this
// directory. Hundreds of references across the tree name those paths —
// `docs/features/whatsapp.md` in Ansible defaults, in docker-compose comments,
// in `.claude/rules/`, in source docstrings — so the files stay where they are
// and the site points at them.
const config: Config = {
  title: 'Istota',
  tagline: 'Self-hosted personal AI operating system',
  url: 'https://istota.cynium.com',
  baseUrl: '/docs/',

  // Directory URLs, as mkdocs emitted them, so every existing link keeps
  // working — the README's are all `/docs/deployment/security/` with the
  // slash. It also matches how the site is served: the ingress strips the
  // `/docs/` prefix and the container serves the build at its root, where
  // nginx would otherwise 301 each slashless path onto its directory.
  trailingSlash: true,

  organizationName: 'istota-project',
  projectName: 'istota',

  // The build is the link audit: 193 relative `.md` links and their anchors
  // are checked here and nowhere else.
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  favicon: 'img/favicon.svg',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
    // `.md` is CommonMark, `.mdx` is MDX. Not the default: under MDX every
    // `{BOT_NAME}` placeholder and every `<your-token>` in prose is a compile
    // error, and both are ordinary throughout these files.
    format: 'detect',
    mermaid: true,
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: '../docs',
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          // The function form, not a string: with `path: '../docs'` the string
          // form is joined with that literal and every link comes out as
          // `.../edit/main/../docs/features/talk.md`. `docPath` is already
          // relative to the docs directory.
          editUrl: ({docPath}) =>
            `https://github.com/istota-project/istota/edit/main/docs/${docPath}`,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    navbar: {
      title: 'Istota',
      items: [
        {type: 'doc', docId: 'getting-started/quickstart-docker', label: 'Getting started', position: 'left'},
        {type: 'doc', docId: 'architecture/overview', label: 'Architecture', position: 'left'},
        {type: 'doc', docId: 'features/skills', label: 'Features', position: 'left'},
        {to: '/messaging', label: 'Messaging', position: 'left'},
        {type: 'doc', docId: 'configuration/overview', label: 'Configuration', position: 'left'},
        {type: 'doc', docId: 'reference/cli', label: 'Reference', position: 'left'},
        {
          href: 'https://github.com/istota-project/istota',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      copyright: 'Istota. Licensed under the European Union Public Licence 1.2.',
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'toml', 'ini', 'yaml', 'json', 'python', 'sql', 'nginx', 'docker'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
