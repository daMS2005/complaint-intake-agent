# Sign-in uses the project's Identity Platform with the Email/Password provider enabled. That config is
# project-wide (and shared in this project), so it is a prerequisite rather than managed here:
#   Console > Identity Platform > Providers > Email/Password.
# The browser needs an API key; this one can only call the two sign-in APIs.
resource "google_apikeys_key" "web" {
  name         = "northwind-resolve-web"
  display_name = "Northwind Resolve browser sign-in"
  restrictions {
    api_targets { service = "identitytoolkit.googleapis.com" }
    api_targets { service = "securetoken.googleapis.com" }
  }
  depends_on = [google_project_service.apis]
}
