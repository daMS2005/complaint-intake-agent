# Dialogflow CX agent whose start flow hands every turn to the ADK agent on Cloud Run.

resource "google_dialogflow_cx_agent" "intake" {
  display_name          = "Northwind complaint intake"
  location              = "global"
  default_language_code = "en"
  time_zone             = "America/New_York"
  depends_on            = [google_project_service.apis]
}

resource "google_dialogflow_cx_webhook" "adk" {
  parent       = google_dialogflow_cx_agent.intake.id
  display_name = "adk-agent"
  timeout      = "30s" # default 5s is too short for an LLM turn with a tool call
  generic_web_service {
    uri                = "${google_cloud_run_v2_service.app.uri}/dialogflow/webhook"
    service_agent_auth = "ID_TOKEN" # Google-signed token the app checks, so only this agent can call it
  }
}

resource "google_dialogflow_cx_flow" "start" {
  parent                = google_dialogflow_cx_agent.intake.id
  display_name          = "Default Start Flow"
  is_default_start_flow = true

  # Greetings and anything CX does not match both go to the webhook.
  transition_routes {
    intent = "${google_dialogflow_cx_agent.intake.id}/intents/00000000-0000-0000-0000-000000000000"
    trigger_fulfillment {
      webhook = google_dialogflow_cx_webhook.adk.id
      tag     = "adk"
    }
  }
  event_handlers {
    event = "sys.no-match-default"
    trigger_fulfillment {
      webhook = google_dialogflow_cx_webhook.adk.id
      tag     = "adk"
    }
  }
  event_handlers {
    event = "sys.no-input-default"
    trigger_fulfillment {
      webhook = google_dialogflow_cx_webhook.adk.id
      tag     = "adk"
    }
  }
}

output "dialogflow_agent" { value = google_dialogflow_cx_agent.intake.id }
