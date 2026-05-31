resource "google_compute_global_address" "default" {
  name    = "hf-lb-ip"
  project = var.project

  depends_on = [google_project_service.apis]
}

resource "google_compute_managed_ssl_certificate" "default" {
  name    = "hf-ssl"
  project = var.project

  managed {
    domains = ["humanflourish.ing"]
  }

  depends_on = [google_project_service.apis]
}

resource "google_compute_region_network_endpoint_group" "cloudrun" {
  name                  = "hf-neg"
  project               = var.project
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.app.name
  }
}

resource "google_compute_security_policy" "default" {
  name    = "hf-armor"
  project = var.project

  depends_on = [google_project_service.apis]

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "Default allow — replace with WAF rules before public launch"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_backend_service" "default" {
  name                  = "hf-backend"
  project               = var.project
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.default.id

  backend {
    group = google_compute_region_network_endpoint_group.cloudrun.id
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_compute_url_map" "default" {
  name            = "hf-urlmap"
  project         = var.project
  default_service = google_compute_backend_service.default.id
}

resource "google_compute_url_map" "http_redirect" {
  name    = "hf-http-redirect"
  project = var.project

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_https_proxy" "default" {
  name             = "hf-https-proxy"
  project          = var.project
  url_map          = google_compute_url_map.default.id
  ssl_certificates = [google_compute_managed_ssl_certificate.default.id]
}

resource "google_compute_target_http_proxy" "redirect" {
  name    = "hf-http-proxy"
  project = var.project
  url_map = google_compute_url_map.http_redirect.id
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "hf-https"
  project               = var.project
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_range            = "443"
  ip_address            = google_compute_global_address.default.address
  target                = google_compute_target_https_proxy.default.id
}

resource "google_compute_global_forwarding_rule" "http" {
  name                  = "hf-http"
  project               = var.project
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_range            = "80"
  ip_address            = google_compute_global_address.default.address
  target                = google_compute_target_http_proxy.redirect.id
}
