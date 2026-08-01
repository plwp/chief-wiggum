// Order detail page — deliberately carries NO @cw-trace annotation, so
// `orient` must bind it to the ui-spec.json route via the artifact-derived
// (inferred) path, not an annotation.

export default function OrderDetailPage({ orderId }: { orderId: string }) {
  return <div className="order-detail">Order {orderId}</div>;
}
