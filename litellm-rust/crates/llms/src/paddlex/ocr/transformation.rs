use crate::{
    base_llm::ocr::{
        error::Error,
        handler::OcrClient,
        transformation::{BaseOcrConfig, PreparedOcrRequest},
    },
    mistral::ocr::transformation::{MistralOcrConfig, MistralOcrRequest},
};
use litellm_core_utils::{call_arguments::CallArguments, params::OpaqueParams};
use litellm_http::request::has_header;
use litellm_llms_types::formats::ocr::{LiteLLMOcrResponse, OcrDocument, OcrResponseFormat};

#[derive(Clone, Debug, Default)]
pub struct PaddlexOcrConfig;

impl BaseOcrConfig for PaddlexOcrConfig {
    type OcrParams = OpaqueParams;
    type ProviderRequest = MistralOcrRequest;
    type Environment = Vec<(String, String)>;

    fn get_supported_ocr_params(&self, model: &str) -> &'static [&'static str] {
        MistralOcrConfig.get_supported_ocr_params(model)
    }

    fn secret_names(&self) -> Vec<&'static str> {
        Vec::new()
    }

    fn map_ocr_params(
        &self,
        non_default_params: &CallArguments,
        _model: &str,
    ) -> Result<OpaqueParams, Error> {
        Ok(non_default_params.clone().into_iter().collect())
    }

    async fn validate_environment(
        &self,
        request: &PreparedOcrRequest,
        _client: &OcrClient,
    ) -> Result<Self::Environment, Error> {
        let connection = &request.connection;
        let authorization = connection
            .api_key
            .as_ref()
            .map(|key| key.expose().to_string())
            .filter(|key| !key.is_empty());
        let supplied = authorization.map(|key| ("Authorization".into(), format!("Bearer {key}")));
        Ok(if has_header(&connection.extra_headers, "authorization") {
            connection.extra_headers.clone()
        } else {
            supplied
                .into_iter()
                .chain(connection.extra_headers.clone())
                .collect()
        })
    }

    fn get_complete_url(
        &self,
        request: &PreparedOcrRequest,
        optional_params: &Self::OcrParams,
        environment: &Self::Environment,
    ) -> Result<String, Error> {
        MistralOcrConfig.get_complete_url(request, optional_params, environment)
    }

    fn transform_ocr_request(
        &self,
        model: &str,
        document: OcrDocument,
        optional_params: &Self::OcrParams,
        headers: &[(String, String)],
    ) -> Result<Self::ProviderRequest, Error> {
        MistralOcrConfig.transform_ocr_request(model, document, optional_params, headers)
    }

    fn transform_ocr_response(
        &self,
        model: &str,
        raw_response: &[u8],
        request_format: OcrResponseFormat,
    ) -> Result<LiteLLMOcrResponse, Error> {
        MistralOcrConfig.transform_ocr_response(model, raw_response, request_format)
    }
}
