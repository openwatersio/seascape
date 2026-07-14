// vt-pbf ships no types. We use only fromVectorTileJs: it takes a tile-like
// object ({ layers: { name → layer } }, each layer exposing extent/version/name/
// length + feature(i)) and returns the serialized MVT bytes.
declare module "vt-pbf" {
  interface VtPbf {
    fromVectorTileJs(tile: { layers: Record<string, unknown> }): Uint8Array;
  }
  const vtpbf: VtPbf;
  export default vtpbf;
}
