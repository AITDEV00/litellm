package dispatchpb

// Runs from the module root so the generated `source:` path stays
// dispatchpb/dispatch.proto, matching the committed stubs. Generating from
// inside this directory rewrites it to dispatch.proto and every regen shows a
// spurious diff.
//go:generate sh -c "cd .. && protoc --go_out=. --go_opt=paths=source_relative --go-grpc_out=. --go-grpc_opt=paths=source_relative dispatchpb/dispatch.proto"
