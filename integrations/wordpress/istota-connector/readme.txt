=== Istota Connector ===
Requires at least: 6.9
Requires PHP: 7.4
Stable tag: 0.1.0
License: EUPL-1.2

Abilities the Istota wordpress skill uses where core REST has no route.

== Description ==

Registers three abilities with the WordPress Abilities API (WordPress 6.9 and later):

* istota/options-get reads the fields of an ACF options page. Needs manage_options and the page's own capability.
* istota/options-update writes those fields, each value replaced whole. Same permission.
* istota/network-sites lists a multisite network's sites. Needs manage_sites on the main site.

Only fields in a field group with "Show in REST API" switched on are reachable. The plugin has no settings, stores nothing, and adds no REST routes of its own.

== Installation ==

Build the zip with scripts/build-wordpress-connector.sh in the Istota repository, then upload it in wp-admin under Plugins, Add New, Upload Plugin. On a multisite network, network-activate it.
