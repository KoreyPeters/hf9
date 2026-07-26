terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

# `billing_project` + `user_project_override` are required by the Billing Budgets
# API, which refuses requests from user credentials that carry no quota project.
# Setting it on ADC alone is not enough — the provider has to send it per call.
provider "google" {
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}

provider "google-beta" {
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}
