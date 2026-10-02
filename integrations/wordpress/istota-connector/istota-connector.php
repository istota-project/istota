<?php
/**
 * Plugin Name: Istota Connector
 * Description: Registers three Abilities API abilities the Istota wordpress skill uses where core REST has no route: read and write an ACF options page, and list a multisite network's sites.
 * Version: 0.1.0
 * Requires at least: 6.9
 * Requires PHP: 7.4
 * Author: Istota
 * License: EUPL-1.2
 * Network: true
 * Text Domain: istota-connector
 *
 * Every ability is registered with an input and output schema, a permission
 * callback and annotations, and shown in REST, so the skill reaches it through
 * /wp-abilities/v1/abilities/{name}/run like any other ability.
 *
 * - istota/options-get: the fields of one ACF options page. manage_options,
 *   plus the page's own capability.
 * - istota/options-update: write those fields, each value replaced whole.
 *   Same permission. Not destructive: it overwrites values, it removes nothing.
 * - istota/network-sites: the network's sites. manage_sites on the main site.
 *
 * Only fields in a field group whose "Show in REST API" setting is on are
 * reachable, the same rule ACF applies to posts over REST. A field in any
 * other group can be neither read nor written through these abilities.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const ISTOTA_CONNECTOR_MAX_SITES = 1000;

add_action( 'wp_abilities_api_categories_init', 'istota_connector_register_category' );
add_action( 'wp_abilities_api_init', 'istota_connector_register_abilities' );

function istota_connector_register_category() {
	wp_register_ability_category(
		'istota',
		array(
			'label'       => 'Istota',
			'description' => 'Abilities the Istota assistant uses where core REST has no route.',
		)
	);
}

/**
 * The options page named by $slug, or a WP_Error.
 */
function istota_connector_options_page( $slug ) {
	if ( ! function_exists( 'acf_get_options_page' ) || ! function_exists( 'acf_get_field_groups' ) ) {
		return new WP_Error( 'istota_acf_missing', 'ACF or Secure Custom Fields with options pages is not active on this site.', array( 'status' => 400 ) );
	}
	$page = acf_get_options_page( $slug );
	if ( empty( $page ) || ! is_array( $page ) ) {
		return new WP_Error( 'istota_options_page_not_found', 'There is no options page with that slug.', array( 'status' => 404 ) );
	}
	return $page;
}

/**
 * The page's top-level fields, by name, from REST-visible field groups only.
 */
function istota_connector_rest_fields( $slug ) {
	$fields = array();
	foreach ( acf_get_field_groups( array( 'options_page' => $slug ) ) as $group ) {
		if ( empty( $group['show_in_rest'] ) ) {
			continue;
		}
		foreach ( acf_get_fields( $group ) as $field ) {
			if ( ! empty( $field['name'] ) ) {
				$fields[ $field['name'] ] = $field;
			}
		}
	}
	return $fields;
}

/**
 * A field's value in the shape ACF's REST API gives it on a post.
 */
function istota_connector_field_value( $field, $post_id ) {
	if ( function_exists( 'acf_format_value_for_rest' ) ) {
		$format = function_exists( 'acf_get_setting' ) ? acf_get_setting( 'rest_api_format' ) : 'light';
		return acf_format_value_for_rest( acf_get_value( $post_id, $field ), $post_id, $field, $format );
	}
	return get_field( $field['key'], $post_id, false );
}

function istota_connector_page_values( $slug, $post_id ) {
	$values = array();
	foreach ( istota_connector_rest_fields( $slug ) as $name => $field ) {
		$values[ $name ] = istota_connector_field_value( $field, $post_id );
	}
	return $values;
}

/**
 * manage_options on this site, and the options page's own capability.
 */
function istota_connector_can_edit_options( $input = null ) {
	if ( ! current_user_can( 'manage_options' ) ) {
		return false;
	}
	if ( is_array( $input ) && isset( $input['page'] ) && is_string( $input['page'] ) ) {
		$page = istota_connector_options_page( $input['page'] );
		if ( is_array( $page ) && ! empty( $page['capability'] ) && ! current_user_can( $page['capability'] ) ) {
			return false;
		}
	}
	return true;
}

function istota_connector_can_list_sites() {
	if ( ! is_multisite() ) {
		return new WP_Error( 'istota_not_multisite', 'This site is not a multisite network.', array( 'status' => 400 ) );
	}
	return current_user_can_for_site( get_main_site_id(), 'manage_sites' );
}

function istota_connector_options_get( $input ) {
	$page = istota_connector_options_page( $input['page'] );
	if ( is_wp_error( $page ) ) {
		return $page;
	}
	return array(
		'page'    => $input['page'],
		'post_id' => (string) $page['post_id'],
		'fields'  => (object) istota_connector_page_values( $input['page'], $page['post_id'] ),
	);
}

function istota_connector_options_update( $input ) {
	$page = istota_connector_options_page( $input['page'] );
	if ( is_wp_error( $page ) ) {
		return $page;
	}
	$fields = istota_connector_rest_fields( $input['page'] );

	// Every name and every value is checked before anything is written.
	$invalid = array();
	foreach ( $input['fields'] as $name => $value ) {
		if ( ! isset( $fields[ $name ] ) ) {
			$invalid[ $name ] = 'Not a REST-visible field of this options page.';
			continue;
		}
		$type = function_exists( 'acf_get_field_type' ) ? acf_get_field_type( $fields[ $name ]['type'] ) : null;
		if ( $type && method_exists( $type, 'get_rest_schema' ) ) {
			$checked = rest_validate_value_from_schema( $value, $type->get_rest_schema( $fields[ $name ] ), $name );
			if ( is_wp_error( $checked ) ) {
				$invalid[ $name ] = $checked->get_error_message();
			}
		}
	}
	if ( $invalid ) {
		return new WP_Error(
			'rest_invalid_param',
			'Invalid field(s): ' . implode( ', ', array_keys( $invalid ) ),
			array(
				'status' => 400,
				'params' => $invalid,
			)
		);
	}

	foreach ( $input['fields'] as $name => $value ) {
		update_field( $fields[ $name ]['key'], $value, $page['post_id'] );
	}
	return array(
		'page'    => $input['page'],
		'post_id' => (string) $page['post_id'],
		'fields'  => (object) istota_connector_page_values( $input['page'], $page['post_id'] ),
	);
}

function istota_connector_network_sites( $input ) {
	$number = ISTOTA_CONNECTOR_MAX_SITES;
	if ( is_array( $input ) && isset( $input['number'] ) ) {
		$number = max( 1, min( ISTOTA_CONNECTOR_MAX_SITES, absint( $input['number'] ) ) );
	}
	$sites = array();
	foreach ( get_sites( array( 'number' => $number, 'network_id' => get_current_network_id() ) ) as $site ) {
		$sites[] = array(
			'id'       => (int) $site->blog_id,
			'domain'   => $site->domain,
			'path'     => $site->path,
			'name'     => (string) $site->blogname,
			'public'   => (bool) $site->public,
			'archived' => (bool) $site->archived,
			'deleted'  => (bool) $site->deleted,
		);
	}
	return array(
		'total' => (int) get_sites( array( 'count' => true, 'network_id' => get_current_network_id() ) ),
		'sites' => $sites,
	);
}

function istota_connector_page_schema() {
	return array(
		'type'        => 'string',
		'description' => 'The options page slug, such as acf-options.',
		'pattern'     => '^[A-Za-z0-9_-]{1,64}$',
	);
}

function istota_connector_page_output_schema() {
	return array(
		'type'       => 'object',
		'properties' => array(
			'page'    => array( 'type' => 'string' ),
			'post_id' => array( 'type' => 'string' ),
			'fields'  => array(
				'type'                 => 'object',
				'additionalProperties' => true,
			),
		),
	);
}

function istota_connector_register_abilities() {
	wp_register_ability(
		'istota/options-get',
		array(
			'label'               => 'Read an ACF options page',
			'description'         => 'The values of an ACF options page\'s fields, from field groups shown in REST.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array( 'page' => istota_connector_page_schema() ),
				'required'             => array( 'page' ),
				'additionalProperties' => false,
			),
			'output_schema'       => istota_connector_page_output_schema(),
			'execute_callback'    => 'istota_connector_options_get',
			'permission_callback' => 'istota_connector_can_edit_options',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => true,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);

	wp_register_ability(
		'istota/options-update',
		array(
			'label'               => 'Write ACF options page fields',
			'description'         => 'Replace the values of the named fields of an ACF options page, from field groups shown in REST. Other fields are left alone.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array(
					'page'   => istota_connector_page_schema(),
					'fields' => array(
						'type'                 => 'object',
						'minProperties'        => 1,
						'additionalProperties' => true,
					),
				),
				'required'             => array( 'page', 'fields' ),
				'additionalProperties' => false,
			),
			'output_schema'       => istota_connector_page_output_schema(),
			'execute_callback'    => 'istota_connector_options_update',
			'permission_callback' => 'istota_connector_can_edit_options',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => false,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);

	wp_register_ability(
		'istota/network-sites',
		array(
			'label'               => 'List the network\'s sites',
			'description'         => 'The sites of this multisite network: id, domain, path, name, and whether each is public, archived or deleted.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array(
					'number' => array(
						'type'    => 'integer',
						'minimum' => 1,
						'maximum' => ISTOTA_CONNECTOR_MAX_SITES,
					),
				),
				'additionalProperties' => false,
				'default'              => array(),
			),
			'output_schema'       => array(
				'type'       => 'object',
				'properties' => array(
					'total' => array( 'type' => 'integer' ),
					'sites' => array(
						'type'  => 'array',
						'items' => array(
							'type'       => 'object',
							'properties' => array(
								'id'       => array( 'type' => 'integer' ),
								'domain'   => array( 'type' => 'string' ),
								'path'     => array( 'type' => 'string' ),
								'name'     => array( 'type' => 'string' ),
								'public'   => array( 'type' => 'boolean' ),
								'archived' => array( 'type' => 'boolean' ),
								'deleted'  => array( 'type' => 'boolean' ),
							),
						),
					),
				),
			),
			'execute_callback'    => 'istota_connector_network_sites',
			'permission_callback' => 'istota_connector_can_list_sites',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => true,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);
}
