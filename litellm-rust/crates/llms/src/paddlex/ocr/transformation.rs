use std::sync::LazyLock;

use litellm_core_utils::{call_arguments::CallArguments, params::OpaqueParams};

use crate::{
    base_llm::ocr::{
        error::Error,
        handler::OcrClient,
        transformation::{
            BaseOcrConfig, LiteLLMOcrResponse, OcrDocument, OcrResponseFormat, PreparedOcrRequest,
        },
    },
    mistral::ocr::transformation::{MistralOcrConfig, MistralOcrRequest},
};

const PADDLEX_EXTENSION_OCR_PARAMS: &[&str] = &["include_native_labels", "threshold"];

static PADDLEX_OCR_PARAMS: LazyLock<Vec<&'static str>> = LazyLock::new(|| {
    MistralOcrConfig
        .get_supported_ocr_params("")
        .iter()
        .copied()
        .chain(PADDLEX_EXTENSION_OCR_PARAMS.iter().copied())
        .collect()
});

#[derive(Clone, Debug, Default)]
pub struct PaddlexOcrConfig;

impl BaseOcrConfig for PaddlexOcrConfig {
    type OcrParams = OpaqueParams;
    type ProviderRequest = MistralOcrRequest;
    type Environment = Vec<(String, String)>;

    fn get_supported_ocr_params(&self, _model: &str) -> &'static [&'static str] {
        PADDLEX_OCR_PARAMS.as_slice()
    }

    fn secret_names(&self) -> Vec<&'static str> {
        Vec::new()
    }

    fn map_ocr_params(
        &self,
        non_default_params: &CallArguments,
        model: &str,
    ) -> Result<OpaqueParams, Error> {
        Ok(non_default_params
            .select(self.get_supported_ocr_params(model))
            .into())
    }

    async fn validate_environment(
        &self,
        request: &PreparedOcrRequest,
        _client: &OcrClient,
    ) -> Result<Self::Environment, Error> {
        Ok(request.connection.extra_headers.clone())
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
        optional_params: &OpaqueParams,
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

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::base_llm::ocr::transformation::OcrDocument;

    #[test]
    fn supported_params_include_every_mistral_field_plus_the_paddlex_extensions() {
        let params = PaddlexOcrConfig.get_supported_ocr_params("PP-DocLayoutV3");
        for mistral in MistralOcrConfig.get_supported_ocr_params("") {
            assert!(params.contains(mistral), "dropped Mistral param {mistral}");
        }
        for extension in PADDLEX_EXTENSION_OCR_PARAMS {
            assert!(params.contains(extension), "dropped extension {extension}");
        }
    }

    #[test]
    fn extension_params_survive_the_allow_list_filter_and_unknown_fields_do_not() {
        let arguments: CallArguments = serde_json::from_value(json!({
            "threshold": 0.7,
            "include_native_labels": true,
            "unknown_provider_field": "dropped",
        }))
        .unwrap();

        let mapped = PaddlexOcrConfig
            .map_ocr_params(&arguments, "PP-DocLayoutV3")
            .unwrap();

        assert_eq!(mapped.get("threshold"), Some(&json!(0.7)));
        assert_eq!(mapped.get("include_native_labels"), Some(&json!(true)));
        assert_eq!(mapped.get("unknown_provider_field"), None);
    }

    #[test]
    fn no_api_key_is_required_and_no_secret_is_declared() {
        assert_eq!(PaddlexOcrConfig.get_api_key_env_var(), None);
        assert!(PaddlexOcrConfig.secret_names().is_empty());
    }

    #[test]
    fn request_reuses_the_mistral_wire_shape_and_forwards_extension_fields() {
        let document: OcrDocument = serde_json::from_value(
            json!({"type":"document_url","document_url":"https://example.com/a.png"}),
        )
        .unwrap();
        let params: OpaqueParams = serde_json::from_value(json!({"threshold": 0.5})).unwrap();

        let body = PaddlexOcrConfig
            .transform_ocr_request("PP-DocLayoutV3", document, &params, &[])
            .unwrap();

        let value = serde_json::to_value(&body).unwrap();
        assert_eq!(value["model"], json!("PP-DocLayoutV3"));
        assert_eq!(value["threshold"], json!(0.5));
    }

    #[test]
    fn response_decoding_is_the_mistral_one() {
        let payload = serde_json::to_vec(&json!({
            "pages": [{"index": 0, "markdown": "# hi"}],
            "model": "PP-DocLayoutV3",
        }))
        .unwrap();

        let response = PaddlexOcrConfig
            .transform_ocr_response("PP-DocLayoutV3", &payload, OcrResponseFormat::Litellm)
            .unwrap();

        assert_eq!(response.pages.len(), 1);
    }
}
