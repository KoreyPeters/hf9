variable "project" {
  type    = string
  default = "human-flourishing-4"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "app_image" {
  type    = string
  default = "us-central1-docker.pkg.dev/human-flourishing-4/hf/hf-app:latest"
}
