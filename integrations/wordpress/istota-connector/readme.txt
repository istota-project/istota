=== Istota Connector ===
Requires at least: 6.9
Requires PHP: 7.4
Stable tag: 0.2.0
License: EUPL-1.2

Abilities the Istota wordpress skill uses where core REST has no route.

== Description ==

Registers five abilities with the WordPress Abilities API (WordPress 6.9 and later):

* istota/options-get reads the fields of an ACF options page. Needs manage_options and the page's own capability.
* istota/options-update writes those fields, each value replaced whole. Same permission.
* istota/network-sites lists a multisite network's sites. Needs manage_sites on the main site.
* istota/fields-get reads the ACF fields of a post or options page for editing: the field list with a token for each, or the value and definition of one node addressed by a path such as blocks/0/items/3/label. Needs edit_post on the post, or the options page permission above.
* istota/fields-edit applies set, insert, remove and move operations to one top-level field, refused when the token no longer matches what is stored. Only the values the operations write are validated, and nothing they do not name changes, disabled flexible content rows included. Same permission as istota/fields-get.

Only fields in a field group with "Show in REST API" switched on are reachable. The plugin has no settings, stores nothing, and adds no REST routes of its own.

== Installation ==

Build the zip with scripts/build-wordpress-connector.sh in the Istota repository, then upload it in wp-admin under Plugins, Add New, Upload Plugin. On a multisite network, network-activate it.

== Changelog ==

= 0.2.0 =
* Add istota/fields-get and istota/fields-edit: path-level editing of ACF values on posts and options pages.

= 0.1.0 =
* First release: istota/options-get, istota/options-update, istota/network-sites.
