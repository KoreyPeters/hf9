terraform {
  backend "gcs" {
    bucket = "hf-tfstate-human-flourishing-4"
    prefix = "terraform/state"
  }
}
