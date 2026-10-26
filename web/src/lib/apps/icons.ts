/**
 * The shell's icon set for apps: the names a manifest's `icon` may use.
 *
 * The manifest names an icon rather than shipping one, so the sidebar and the
 * cards draw it with the shell's own glyphs. A name not listed here falls back
 * to `DEFAULT_APP_ICON` rather than failing, as the manifest rules say.
 */
import type { Component } from 'svelte';
import {
  AppWindow,
  Blocks,
  BookOpen,
  Brain,
  Calendar,
  ChartBar,
  ChartLine,
  Clock,
  FlaskConical,
  Kanban,
  LayoutDashboard,
  LayoutGrid,
  ListChecks,
  Map as MapIcon,
  Moon,
  Network,
  Notebook,
  Package,
  Scale,
  Sparkles,
  Star,
  Sun,
  Target,
} from '@lucide/svelte';

export const DEFAULT_APP_ICON: Component = AppWindow;

const APP_ICONS: Record<string, Component> = {
  app: AppWindow,
  blocks: Blocks,
  book: BookOpen,
  brain: Brain,
  calendar: Calendar,
  'chart-bar': ChartBar,
  'chart-line': ChartLine,
  clock: Clock,
  flask: FlaskConical,
  kanban: Kanban,
  layout: LayoutDashboard,
  grid: LayoutGrid,
  'list-checks': ListChecks,
  map: MapIcon,
  moon: Moon,
  network: Network,
  notebook: Notebook,
  package: Package,
  scale: Scale,
  sparkles: Sparkles,
  star: Star,
  sun: Sun,
  target: Target,
};

export function appIcon(name: string | null | undefined): Component {
  return (name && Object.hasOwn(APP_ICONS, name) && APP_ICONS[name]) || DEFAULT_APP_ICON;
}
